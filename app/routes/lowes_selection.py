# -*- coding: utf-8 -*-
"""Lowes 选品候选池页面（/lowes-selection）。Autool=豪雅 / Yasonic=司顺，单店重建。"""
import threading
from flask import Blueprint, current_app, jsonify, render_template, request

from app.models.db_manager import DBManager

lowes_selection_bp = Blueprint("lowes_selection", __name__)

_REBUILD = {"autool": False, "yasonic": False}
STORES = {"autool": "Lowes-Autool（豪雅）", "yasonic": "Lowes-Yasonic（司顺）"}
SUPPLIER_CN = {"autool": "豪雅", "yasonic": "司顺"}   # 供应商中文名(面板文案用)


def _query(sql, params=None):
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params) if params else cur.execute(sql)
            return cur.fetchall() or []
    except Exception as exc:
        if "doesn't exist" in str(exc):
            return []
        raise
    finally:
        conn.close()


def _cur_store():
    s = (request.args.get("store") or "autool").strip().lower()
    return s if s in STORES else "autool"


@lowes_selection_bp.route("/")
def page():
    store = _cur_store()
    f_leaf = (request.args.get("leaf") or "").strip()
    f_q = (request.args.get("q") or "").strip()
    f_img = (request.args.get("img") or "").strip()
    f_newn = (request.args.get("newn") or "").strip()   # ''/new/restock
    try:
        pg = max(1, int(request.args.get("page") or 1))
    except (TypeError, ValueError):
        pg = 1
    per = 60

    where, params = ["store=%s"], [store]
    if f_leaf:
        where.append("lowes_leaf=%s"); params.append(f_leaf)
    if f_q:
        where.append("(supplier_sku LIKE %s OR title LIKE %s OR supplier_cat LIKE %s OR lowes_path LIKE %s)")
        params += [f"%{f_q}%"] * 4
    if f_img == "y":
        where.append("has_overview_img=1")
    elif f_img == "n":
        where.append("has_overview_img=0")
    if f_newn == "new":
        where.append("is_new=1")
    elif f_newn == "restock":
        where.append("is_restock=1")
    w = " AND ".join(where)

    total = int((_query(f"SELECT COUNT(*) n FROM order_system.lowes_selection_pool WHERE {w}",
                        tuple(params)) or [{"n": 0}])[0]["n"])
    pages = max(1, (total + per - 1) // per)
    pg = min(pg, pages)
    rows = _query(f"""SELECT * FROM order_system.lowes_selection_pool WHERE {w}
                      ORDER BY heat_90d DESC, stock DESC LIMIT %s OFFSET %s""",
                  tuple(params) + (per, (pg - 1) * per))
    leaves = [r["lowes_leaf"] for r in _query(
        """SELECT lowes_leaf, COUNT(*) n FROM order_system.lowes_selection_pool
           WHERE store=%s AND lowes_leaf IS NOT NULL GROUP BY lowes_leaf ORDER BY n DESC""",
        (store,))]
    imgc = _query("""SELECT SUM(has_overview_img=1) y, SUM(has_overview_img=0) n
                     FROM order_system.lowes_selection_pool WHERE store=%s""", (store,))
    img_stat = imgc[0] if imgc else {"y": 0, "n": 0}
    nnc = _query("""SELECT COUNT(*) a, SUM(is_new) nw, SUM(is_restock) rs
                    FROM order_system.lowes_selection_pool WHERE store=%s""", (store,))
    newn_stat = nnc[0] if nnc else {"a": 0, "nw": 0, "rs": 0}
    # 当前筛选(店铺+类目+搜索)下的无图SKU数 —— 导出按钮显示,让"按类目导出"一目了然
    nw2, np2 = ["store=%s", "has_overview_img=0"], [store]
    if f_leaf:
        nw2.append("lowes_leaf=%s"); np2.append(f_leaf)
    if f_q:
        nw2.append("(supplier_sku LIKE %s OR title LIKE %s OR supplier_cat LIKE %s OR lowes_path LIKE %s)")
        np2 += [f"%{f_q}%"] * 4
    noimg_n = int((_query(
        f"SELECT COUNT(*) n FROM order_system.lowes_selection_pool WHERE {' AND '.join(nw2)}",
        tuple(np2)) or [{"n": 0}])[0]["n"])
    built = _query("SELECT MAX(rebuilt_at) t FROM order_system.lowes_selection_pool WHERE store=%s",
                   (store,))
    push_log = _query("""SELECT batch_desc, sku_count, leaf_summary, pushed_at
                         FROM order_system.lowes_push_log WHERE store=%s
                         ORDER BY pushed_at DESC LIMIT 50""", (store,))
    store_totals = {r["store"]: int(r["n"]) for r in _query(
        "SELECT store, COUNT(*) n FROM order_system.lowes_selection_pool GROUP BY store")}
    # 类目热度面板：近90天该店卖得最好的类目 + 各有多少候选
    cat_demand = _query("""SELECT d.lowes_leaf, d.gmv, d.units, d.margin_rate,
                                  d.gross_rate, d.ret_rate, d.score,
                                  d.season_tag, d.season_peak, d.trend_now,
                                  COALESCE(p.n,0) AS cand_n
                           FROM order_system.lowes_cat_demand d
                           LEFT JOIN (SELECT lowes_leaf, COUNT(*) n
                                      FROM order_system.lowes_selection_pool
                                      WHERE store=%s GROUP BY lowes_leaf) p
                             ON p.lowes_leaf=d.lowes_leaf
                           WHERE d.store=%s ORDER BY d.score DESC, d.gmv DESC LIMIT 15""",
                        (store, store))
    demand_map = {r["lowes_leaf"]: r for r in _query(
        "SELECT lowes_leaf, gmv, units, margin_rate, gross_rate, ret_rate, score, "
        "season_tag, season_peak, trend_now FROM order_system.lowes_cat_demand WHERE store=%s", (store,))}
    # 蓝海类目：豪雅有货但我们0销量的类目(邻接适配×货盘×季节)
    blue_ocean = _query("""SELECT lowes_leaf, l1, l2, sku_n, with_img, avg_price,
                                  fit_score, fit_reason, supply_score,
                                  season_tag, season_peak, trend_now, blue_score,
                                  amz_units, amz_revenue, amz_price, amz_return
                           FROM order_system.lowes_blue_ocean
                           WHERE store=%s AND blue_score>=50
                           ORDER BY blue_score DESC, sku_n DESC LIMIT 40""", (store,))
    # 探索区：邻接弱(进不了主推)但 Amazon 需求大的类目，平台没把握，人工判断
    explore_ocean = _query("""SELECT lowes_leaf, l1, l2, sku_n, with_img, avg_price, fit_reason,
                                     amz_units, amz_revenue, amz_price, amz_return, amz_node,
                                     season_tag, season_peak, trend_now
                              FROM order_system.lowes_blue_ocean
                              WHERE store=%s AND fit_score<55 AND amz_units IS NOT NULL
                                    AND amz_units>=3000
                              ORDER BY amz_units DESC LIMIT 15""", (store,))
    return render_template("lowes_selection/page.html", rows=rows, total=total,
                           page=pg, pages=pages, per=per, store=store, stores=STORES,
                           f_leaf=f_leaf, f_q=f_q, f_img=f_img, f_newn=f_newn,
                           leaves=leaves, img_stat=img_stat, newn_stat=newn_stat,
                           noimg_n=noimg_n,
                           built_at=built[0]["t"] if built else None,
                           push_log=push_log, store_totals=store_totals,
                           cat_demand=cat_demand, demand_map=demand_map,
                           blue_ocean=blue_ocean, explore_ocean=explore_ocean,
                           supplier_cn=SUPPLIER_CN.get(store, "供应商"),
                           rebuilding=_REBUILD.get(store, False))


@lowes_selection_bp.route("/export-noimg")
def export_noimg():
    """导出当前店铺(+当前类目/搜索筛选)所有「总览无图」候选 → Excel，
    供人工整理图片后上传飞书图片总览表。带供应商图片链接做起点。"""
    from io import BytesIO
    from datetime import datetime
    from openpyxl import Workbook
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    from flask import send_file
    store = _cur_store()
    f_leaf = (request.args.get("leaf") or "").strip()
    f_q = (request.args.get("q") or "").strip()
    where, params = ["store=%s", "has_overview_img=0"], [store]
    if f_leaf:
        where.append("lowes_leaf=%s"); params.append(f_leaf)
    if f_q:
        where.append("(supplier_sku LIKE %s OR title LIKE %s OR supplier_cat LIKE %s OR lowes_path LIKE %s)")
        params += [f"%{f_q}%"] * 4
    w = " AND ".join(where)
    rows = _query(f"""SELECT supplier_sku, title, supplier_cat, lowes_leaf, lowes_path,
                             image, price, brand, heat_90d
                      FROM order_system.lowes_selection_pool WHERE {w}
                      ORDER BY heat_90d DESC, supplier_sku""", tuple(params))

    def _clean(v):
        return ILLEGAL_CHARACTERS_RE.sub("", str(v)) if v not in (None, "") else ""

    wb = Workbook()
    ws = wb.active
    ws.title = "无图SKU"
    ws.append(["供应商SKU", "产品名", "供应商类目", "建议Lowes类目", "店铺类目(完整路径)",
               "供应商图片链接", "价格", "品牌", "推荐分"])
    for r in rows:
        ws.append([_clean(r["supplier_sku"]), _clean(r["title"]), _clean(r["supplier_cat"]),
                   _clean(r["lowes_leaf"]), _clean(r["lowes_path"]), _clean(r["image"]),
                   _clean(r["price"]), _clean(r["brand"]), r["heat_90d"]])
    ws["K1"] = f"共{len(rows)}个无图SKU"
    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)
    fname = f"lowes_{store}_无图SKU_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return send_file(bio, as_attachment=True, download_name=fname,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@lowes_selection_bp.route("/rebuild", methods=["POST"])
def rebuild():
    store = (request.form.get("store") or request.args.get("store") or "autool").strip().lower()
    if store not in STORES:
        return jsonify({"success": False, "msg": "未知店铺"})
    if _REBUILD.get(store):
        return jsonify({"success": False, "msg": f"{STORES[store]} 正在重建中，请稍候"})
    _REBUILD[store] = True
    app_obj = current_app._get_current_object()

    def _bg():
        try:
            with app_obj.app_context():
                from app.services.lowes_selection_service import rebuild_pool
                print(f"[lowes_selection] rebuild {store}:", rebuild_pool(store))
        except Exception as exc:
            print(f"[lowes_selection] rebuild {store} failed:", exc)
        finally:
            _REBUILD[store] = False

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({"success": True, "msg": f"{STORES[store]} 候选池后台重建中，几分钟后刷新"})


@lowes_selection_bp.route("/push", methods=["POST"])
def push():
    from app.services.lowes_selection_service import push_to_feishu
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
