# -*- coding: utf-8 -*-
"""Lowes 选品候选池页面（/lowes-selection）。Autool=豪雅 / Yasonic=司顺，单店重建。"""
import json
import threading
from datetime import date

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
    # 近3个月最旺类目雷达：按 season_profile 在 [本月,+1,+2] 窗口的平均热度排(在售+蓝海+探索)
    cur_m = date.today().month
    win_idx = [((cur_m - 1 + k) % 12) for k in range(3)]           # 0-indexed 月
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
    for r in _query("SELECT lowes_leaf, season_profile, season_peak, season_tag "
                    "FROM order_system.lowes_cat_demand "
                    "WHERE store=%s AND season_profile IS NOT NULL", (store,)):
        hh = _win_heat(r["season_profile"])
        if hh:
            hot.append({"leaf": r["lowes_leaf"], "kind": "在售", "heat": hh,
                        "peak": r["season_peak"], "tag": r["season_tag"]})
    for r in _query("SELECT lowes_leaf, season_profile, season_peak, season_tag, blue_score FROM "
                    "order_system.lowes_blue_ocean WHERE store=%s AND season_profile IS NOT NULL", (store,)):
        hh = _win_heat(r["season_profile"])
        if hh:
            hot.append({"leaf": r["lowes_leaf"],
                        "kind": "蓝海" if (r["blue_score"] or 0) >= 50 else "探索",
                        "heat": hh, "peak": r["season_peak"], "tag": r["season_tag"]})
    hot.sort(key=lambda x: x["heat"], reverse=True)
    hot_window = hot[:12]
    return render_template("lowes_selection/page.html", rows=rows, total=total,
                           page=pg, pages=pages, per=per, store=store, stores=STORES,
                           f_leaf=f_leaf, f_q=f_q, f_img=f_img, f_newn=f_newn,
                           leaves=leaves, img_stat=img_stat, newn_stat=newn_stat,
                           noimg_n=noimg_n,
                           built_at=built[0]["t"] if built else None,
                           push_log=push_log, store_totals=store_totals,
                           cat_demand=cat_demand, demand_map=demand_map,
                           blue_ocean=blue_ocean, explore_ocean=explore_ocean,
                           hot_window=hot_window, win_months=win_months,
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


def _write(statements):
    conn = DBManager.get_connection()
    n = 0
    try:
        with conn.cursor() as cur:
            for sql, params in statements:
                cur.execute(sql, params)
                n += cur.rowcount or 0
        conn.commit()
        return n
    finally:
        conn.close()


def _supplier_of(store):
    return "Costway" if store == "autool" else "Vevor"


def _leaf_of_path(path):
    r = _query("SELECT leaf FROM order_system.lowes_leaf_category WHERE full_path=%s LIMIT 1", (path,))
    if r and r[0].get("leaf"):
        return r[0]["leaf"]
    return path.rsplit("/", 1)[-1] if path else ""


@lowes_selection_bp.route("/triage", methods=["POST"])
def triage():
    """未归类桶:整类映射(记住,含将来新品) / 勾选部分映射(只这些)。"""
    data = request.get_json(silent=True) or {}
    action = (data.get("action") or "").strip()
    store = (data.get("store") or "autool").strip().lower()
    if store not in STORES:
        store = "autool"
    supplier = _supplier_of(store)
    try:
        if action == "apply_category":
            cat = (data.get("supplier_cat") or "").strip()
            path = (data.get("leaf") or "").strip()   # 前端传 full_path
            if not (cat and path):
                return jsonify({"success": False, "msg": "缺供应商类目/目标类目"})
            leaf = _leaf_of_path(path)
            _write([("INSERT INTO order_system.lowes_cat_override "
                     "(store,supplier,supplier_cat,lowes_leaf,lowes_path) VALUES (%s,%s,%s,%s,%s) "
                     "ON DUPLICATE KEY UPDATE lowes_leaf=VALUES(lowes_leaf), lowes_path=VALUES(lowes_path)",
                     (store, supplier, cat, leaf, path))])
            return jsonify({"success": True,
                            "msg": f"已把「{cat[:40]}」整类归到 {leaf} 并记住(含将来新品),重建后进池"})
        if action == "map_skus":
            path = (data.get("leaf") or "").strip()
            skus = [str(x).strip() for x in (data.get("skus") or []) if str(x).strip()]
            if not (path and skus):
                return jsonify({"success": False, "msg": "缺目标类目/勾选产品"})
            leaf = _leaf_of_path(path)
            stmts = [("INSERT INTO order_system.lowes_selection_decision "
                      "(store,supplier,supplier_sku,decision,override_leaf,override_path) "
                      "VALUES (%s,%s,%s,'approved',%s,%s) "
                      "ON DUPLICATE KEY UPDATE override_leaf=VALUES(override_leaf), "
                      "override_path=VALUES(override_path), decision='approved'",
                      (store, supplier, s[:64], leaf, path)) for s in skus]
            _write(stmts)
            return jsonify({"success": True, "n": len(skus),
                            "msg": f"已把勾选的 {len(skus)} 个归到 {leaf}（重建后进池,不含将来新品）"})
        return jsonify({"success": False, "msg": "未知操作"}), 400
    except Exception as exc:
        return jsonify({"success": False, "msg": str(exc)[:200]}), 500


@lowes_selection_bp.route("/unmapped")
def unmapped():
    """🕳 未归类复核:有货>50 但类目没归到 Lowes、不在池里的产品(带图),人工映射进池。"""
    store = _cur_store()
    supplier = _supplier_of(store)
    leaf_options = [r["full_path"] for r in _query(
        "SELECT full_path FROM order_system.lowes_leaf_category WHERE active=1 ORDER BY full_path")]
    f_cat = (request.args.get("cat") or "").strip()

    if f_cat:   # 某类目的具体产品(带图)
        if supplier == "Costway":
            products = _query("""SELECT d.SKU AS sku, c.title, c.image_url AS img, d.Stock AS stock, d.Price AS price
                FROM autooperate.newestdropship d
                JOIN order_system.safety_product_cache c ON c.sku=d.SKU AND c.supplier='Costway'
                WHERE COALESCE(d.`status`,'Enabled')<>'Disabled' AND c.category=%s AND d.Stock>50
                  AND NOT EXISTS(SELECT 1 FROM order_system.lowes_used_sku uu WHERE uu.store=%s AND uu.supplier_sku=d.SKU)
                ORDER BY d.Stock DESC LIMIT 300""", (f_cat, store))
        else:
            products = _query("""SELECT v.sku, v.title, v.image AS img, v.inventory AS stock, v.price
                FROM autooperate.vevor_feed v
                WHERE v.product_type=%s AND v.inventory>50
                  AND NOT EXISTS(SELECT 1 FROM order_system.lowes_used_sku uu WHERE uu.store=%s AND uu.supplier_sku=v.sku)
                ORDER BY v.inventory DESC LIMIT 300""", (f_cat, store))
        return render_template("lowes_selection/unmapped.html", store=store, supplier=supplier,
                               rows=None, products=products, leaf_options=leaf_options, total_n=0, f_cat=f_cat)

    if supplier == "Costway":
        rows = _query("""SELECT c.category AS cat, COUNT(*) AS n, MAX(c.image_url) AS img
            FROM autooperate.newestdropship d
            JOIN order_system.safety_product_cache c ON c.sku=d.SKU AND c.supplier='Costway'
            WHERE COALESCE(d.`status`,'Enabled')<>'Disabled' AND c.category<>'' AND d.Stock>50
              AND NOT EXISTS(SELECT 1 FROM order_system.lowes_cat_map m WHERE m.supplier='Costway' AND m.supplier_cat=c.category AND m.lowes_path IS NOT NULL)
              AND NOT EXISTS(SELECT 1 FROM order_system.lowes_cat_override o WHERE o.store=%s AND o.supplier='Costway' AND o.supplier_cat=c.category)
              AND NOT EXISTS(SELECT 1 FROM order_system.lowes_used_sku uu WHERE uu.store=%s AND uu.supplier_sku=d.SKU)
            GROUP BY c.category ORDER BY n DESC LIMIT 500""", (store, store))
    else:
        rows = _query("""SELECT v.product_type AS cat, COUNT(*) AS n, MAX(v.image) AS img
            FROM autooperate.vevor_feed v
            WHERE v.product_type<>'' AND v.inventory>50
              AND NOT EXISTS(SELECT 1 FROM order_system.lowes_cat_map m WHERE m.supplier='Vevor' AND m.supplier_cat=v.product_type AND m.lowes_path IS NOT NULL)
              AND NOT EXISTS(SELECT 1 FROM order_system.lowes_cat_override o WHERE o.store=%s AND o.supplier='Vevor' AND o.supplier_cat=v.product_type)
              AND NOT EXISTS(SELECT 1 FROM order_system.lowes_used_sku uu WHERE uu.store=%s AND uu.supplier_sku=v.sku)
            GROUP BY v.product_type ORDER BY n DESC LIMIT 500""", (store, store))
    total_n = sum(int(r["n"]) for r in rows)
    return render_template("lowes_selection/unmapped.html", store=store, supplier=supplier,
                           rows=rows, products=None, leaf_options=leaf_options, total_n=total_n, f_cat="")
