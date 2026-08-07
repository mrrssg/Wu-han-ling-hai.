# -*- coding: utf-8 -*-
"""Macy-Kuyotq 选品候选池页面（/macy-selection）。"""
import json
import threading
from datetime import date

from flask import Blueprint, current_app, jsonify, render_template, request

from app.models.db_manager import DBManager

macy_selection_bp = Blueprint("macy_selection", __name__)

_REBUILD = {"running": False}


def _query(sql, params=None):
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params) if params else cur.execute(sql)
            return cur.fetchall() or []
    except Exception as exc:
        if "doesn't exist" in str(exc):   # 候选池未建(未首次rebuild)——当空池
            return []
        raise
    finally:
        conn.close()


@macy_selection_bp.route("/")
def page():
    f_supplier = (request.args.get("supplier") or "").strip()
    f_leaf = (request.args.get("leaf") or "").strip()
    f_q = (request.args.get("q") or "").strip()
    f_img = (request.args.get("img") or "").strip()   # ''全部 / 'y'有图 / 'n'无图
    f_newn = (request.args.get("newn") or "").strip()  # ''/new/restock
    try:
        pg = max(1, int(request.args.get("page") or 1))
    except (TypeError, ValueError):
        pg = 1
    per = 60

    f_store = (request.args.get("store") or "kuyotq").strip().lower()
    if f_store not in ("kuyotq", "wopet"):
        f_store = "kuyotq"
    where, params = ["store=%s"], [f_store]
    if f_supplier:
        where.append("supplier=%s"); params.append(f_supplier)
    if f_leaf:
        where.append("macy_leaf=%s"); params.append(f_leaf)
    if f_q:
        where.append("(supplier_sku LIKE %s OR title LIKE %s OR supplier_cat LIKE %s OR macy_leaf LIKE %s)")
        params += [f"%{f_q}%"] * 4
    if f_img == "y":
        where.append("has_overview_img=1")
    elif f_img == "n":
        where.append("has_overview_img=0")
    if f_newn == "new":
        where.append("is_new=1")
    elif f_newn == "restock":
        where.append("is_restock=1")
    # 主池=🎯AI精选(tier='ai'),下方另有🖐人工待选(tier='manual')
    w = " AND ".join(where) + " AND tier='ai'"

    total = int((_query(f"SELECT COUNT(*) n FROM order_system.macy_selection_pool WHERE {w}",
                        tuple(params)) or [{"n": 0}])[0]["n"])
    pages = max(1, (total + per - 1) // per)
    pg = min(pg, pages)
    rows = _query(f"""SELECT * FROM order_system.macy_selection_pool WHERE {w}
                      ORDER BY heat_90d DESC, stock DESC LIMIT %s OFFSET %s""",
                  tuple(params) + (per, (pg - 1) * per))
    # 🖐人工待选(擦边)池:tier='manual',同筛选,一次给前200条(带建议叶子/擦边原因)
    manual_rows = _query(f"SELECT * FROM order_system.macy_selection_pool "
                         f"WHERE {' AND '.join(where)} AND tier='manual' "
                         f"ORDER BY heat_90d DESC, stock DESC LIMIT 200", tuple(params))
    manual_total = int((_query(f"SELECT COUNT(*) n FROM order_system.macy_selection_pool "
                               f"WHERE {' AND '.join(where)} AND tier='manual'",
                               tuple(params)) or [{"n": 0}])[0]["n"])
    leaves = [r["macy_leaf"] for r in _query(
        """SELECT macy_leaf, COUNT(*) n FROM order_system.macy_selection_pool
           WHERE store=%s AND macy_leaf IS NOT NULL GROUP BY macy_leaf ORDER BY n DESC""", (f_store,))]
    counts = {r["supplier"]: int(r["n"]) for r in _query(
        "SELECT supplier, COUNT(*) n FROM order_system.macy_selection_pool "
        "WHERE store=%s AND tier='ai' GROUP BY supplier", (f_store,))}
    imgc = _query("SELECT SUM(has_overview_img=1) y, SUM(has_overview_img=0) n "
                  "FROM order_system.macy_selection_pool WHERE store=%s AND tier='ai'", (f_store,))
    img_stat = imgc[0] if imgc else {"y": 0, "n": 0}
    nnc = _query("SELECT COUNT(*) a, SUM(is_new) nw, SUM(is_restock) rs "
                 "FROM order_system.macy_selection_pool WHERE store=%s AND tier='ai'", (f_store,))
    newn_stat = nnc[0] if nnc else {"a": 0, "nw": 0, "rs": 0}
    built = _query("SELECT MAX(rebuilt_at) t FROM order_system.macy_selection_pool WHERE store=%s", (f_store,))
    push_log = _query("""SELECT batch_desc, sku_count, costway_n, vevor_n,
                                leaf_summary, pushed_at
                         FROM order_system.macy_push_log
                         ORDER BY pushed_at DESC LIMIT 50""")
    # 类目推荐分面板：近90天净利率×GMV 最高的 Macy 类目 + 各有多少候选
    cat_demand = _query("""SELECT d.macy_leaf, d.gmv, d.units, d.margin_rate, d.gross_rate,
                                  d.comm_rate, d.score, d.season_tag, d.season_peak,
                                  COALESCE(p.n,0) AS cand_n
                           FROM order_system.macy_cat_demand d
                           LEFT JOIN (SELECT macy_leaf, COUNT(*) n
                                      FROM order_system.macy_selection_pool WHERE store=%s
                                      GROUP BY macy_leaf) p
                             ON p.macy_leaf=d.macy_leaf
                           WHERE d.store=%s ORDER BY d.score DESC, d.gmv DESC LIMIT 15""", (f_store, f_store))
    demand_map = {r["macy_leaf"]: r for r in _query(
        "SELECT macy_leaf, gmv, units, margin_rate, gross_rate, comm_rate, score, "
        "season_tag, season_peak FROM order_system.macy_cat_demand WHERE store=%s", (f_store,))}
    # 🌱蓝海类目(邻接强,blue>=50) + 🚀探索区(邻接弱但Amazon需求大)
    blue_ocean = _query("""SELECT macy_leaf, l1, l2, l3, brand, sku_n, with_img, avg_price,
                                  fit_reason, season_tag, season_peak,
                                  amz_units, amz_return, blue_score
                           FROM order_system.macy_blue_ocean
                           WHERE store=%s AND blue_score>=50
                           ORDER BY blue_score DESC, sku_n DESC LIMIT 40""", (f_store,))
    explore_ocean = _query("""SELECT macy_leaf, l1, l2, brand, sku_n, with_img, amz_node,
                                     amz_units, amz_revenue, amz_return, amz_price, season_tag, season_peak
                              FROM order_system.macy_blue_ocean
                              WHERE store=%s AND fit_score<55 AND amz_units>=3000
                              ORDER BY amz_units DESC LIMIT 15""", (f_store,))
    # 📅近3个月最旺雷达(在售+蓝海+探索,按 season_profile 窗口热度)
    cur_m = date.today().month
    win_idx = [((cur_m - 1 + k) % 12) for k in range(3)]
    win_months = "/".join(str(i + 1) for i in win_idx) + "月"

    def _win_heat(pj):
        try:
            p = json.loads(pj) if pj else None
        except (ValueError, TypeError):
            p = None
        if not p or len(p) < 12:
            return None
        vals = [p[i] for i in win_idx if p[i]]
        return round(sum(vals) / len(vals)) if vals else None

    hot = []
    for r in _query("SELECT macy_leaf, season_profile, season_peak, season_tag "
                    "FROM order_system.macy_cat_demand WHERE store=%s AND season_profile IS NOT NULL", (f_store,)):
        hh = _win_heat(r["season_profile"])
        if hh:
            hot.append({"leaf": r["macy_leaf"], "kind": "在售", "heat": hh,
                        "peak": r["season_peak"], "tag": r["season_tag"]})
    for r in _query("SELECT macy_leaf, season_profile, season_peak, season_tag, blue_score "
                    "FROM order_system.macy_blue_ocean WHERE store=%s AND season_profile IS NOT NULL", (f_store,)):
        hh = _win_heat(r["season_profile"])
        if hh:
            hot.append({"leaf": r["macy_leaf"],
                        "kind": "蓝海" if (r["blue_score"] or 0) >= 50 else "探索",
                        "heat": hh, "peak": r["season_peak"], "tag": r["season_tag"]})
    hot.sort(key=lambda x: x["heat"], reverse=True)
    hot_window = hot[:12]
    return render_template("macy_selection/page.html", rows=rows, total=total,
                           page=pg, pages=pages, per=per,
                           f_supplier=f_supplier, f_leaf=f_leaf, f_q=f_q, f_img=f_img,
                           f_newn=f_newn, newn_stat=newn_stat,
                           leaves=leaves, counts=counts, img_stat=img_stat,
                           built_at=built[0]["t"] if built else None,
                           push_log=push_log, cat_demand=cat_demand, demand_map=demand_map,
                           blue_ocean=blue_ocean, explore_ocean=explore_ocean,
                           hot_window=hot_window, win_months=win_months,
                           f_store=f_store, manual_rows=manual_rows, manual_total=manual_total)


@macy_selection_bp.route("/rebuild", methods=["POST"])
def rebuild():
    store = (request.form.get("store") or request.args.get("store") or "kuyotq").strip().lower()
    if store not in ("kuyotq", "wopet"):
        store = "kuyotq"
    if _REBUILD["running"]:
        return jsonify({"success": False, "msg": "正在重建中，请稍候"})
    _REBUILD["running"] = True
    app_obj = current_app._get_current_object()

    def _bg():
        try:
            with app_obj.app_context():
                from app.services.macy_selection_service import rebuild_pool
                print("[macy_selection] rebuild:", rebuild_pool(store))
        except Exception as exc:
            print("[macy_selection] rebuild failed:", exc)
        finally:
            _REBUILD["running"] = False

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({"success": True, "msg": "候选池后台重建中，几分钟后刷新"})


@macy_selection_bp.route("/push", methods=["POST"])
def push():
    from app.services.macy_selection_service import push_to_feishu
    data = request.get_json(silent=True) or {}
    ids = [int(x) for x in (data.get("ids") or []) if str(x).isdigit()]
    batch = (data.get("batch") or "").strip()
    if not ids:
        return jsonify({"success": False, "msg": "没有勾选任何产品"})
    if not batch:
        return jsonify({"success": False, "msg": "请填选品批次描述"})
    try:
        return jsonify(push_to_feishu(ids, batch))
    except Exception as exc:
        return jsonify({"success": False, "msg": str(exc)[:200]}), 500
