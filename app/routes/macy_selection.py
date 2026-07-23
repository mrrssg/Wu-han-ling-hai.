# -*- coding: utf-8 -*-
"""Macy-Kuyotq 选品候选池页面（/macy-selection）。"""
import threading
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
    try:
        pg = max(1, int(request.args.get("page") or 1))
    except (TypeError, ValueError):
        pg = 1
    per = 60

    where, params = ["1=1"], []
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
    w = " AND ".join(where)

    total = int((_query(f"SELECT COUNT(*) n FROM order_system.macy_selection_pool WHERE {w}",
                        tuple(params)) or [{"n": 0}])[0]["n"])
    pages = max(1, (total + per - 1) // per)
    pg = min(pg, pages)
    rows = _query(f"""SELECT * FROM order_system.macy_selection_pool WHERE {w}
                      ORDER BY heat_90d DESC, stock DESC LIMIT %s OFFSET %s""",
                  tuple(params) + (per, (pg - 1) * per))
    leaves = [r["macy_leaf"] for r in _query(
        """SELECT macy_leaf, COUNT(*) n FROM order_system.macy_selection_pool
           GROUP BY macy_leaf ORDER BY n DESC""")]
    counts = {r["supplier"]: int(r["n"]) for r in _query(
        "SELECT supplier, COUNT(*) n FROM order_system.macy_selection_pool GROUP BY supplier")}
    imgc = _query("""SELECT SUM(has_overview_img=1) y, SUM(has_overview_img=0) n
                     FROM order_system.macy_selection_pool""")
    img_stat = imgc[0] if imgc else {"y": 0, "n": 0}
    built = _query("SELECT MAX(rebuilt_at) t FROM order_system.macy_selection_pool")
    push_log = _query("""SELECT batch_desc, sku_count, costway_n, vevor_n,
                                leaf_summary, pushed_at
                         FROM order_system.macy_push_log
                         ORDER BY pushed_at DESC LIMIT 50""")
    return render_template("macy_selection/page.html", rows=rows, total=total,
                           page=pg, pages=pages, per=per,
                           f_supplier=f_supplier, f_leaf=f_leaf, f_q=f_q, f_img=f_img,
                           leaves=leaves, counts=counts, img_stat=img_stat,
                           built_at=built[0]["t"] if built else None,
                           push_log=push_log)


@macy_selection_bp.route("/rebuild", methods=["POST"])
def rebuild():
    if _REBUILD["running"]:
        return jsonify({"success": False, "msg": "正在重建中，请稍候"})
    _REBUILD["running"] = True
    app_obj = current_app._get_current_object()

    def _bg():
        try:
            with app_obj.app_context():
                from app.services.macy_selection_service import rebuild_pool
                print("[macy_selection] rebuild:", rebuild_pool())
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
