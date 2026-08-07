# -*- coding: utf-8 -*-
"""HD(Home Depot)选品候选池页面(/hd-selection)。TOP=厨卫/小家电、BOS=户外/庭院。"""
import threading

from flask import Blueprint, current_app, jsonify, render_template, request

from app.models.db_manager import DBManager

hd_selection_bp = Blueprint("hd_selection", __name__)

_REBUILD = {"running": False}
STORES = ("top", "bos")


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


def _store():
    s = (request.args.get("store") or "top").strip().lower()
    return s if s in STORES else "top"


@hd_selection_bp.route("/")
def page():
    f_store = _store()
    f_supplier = (request.args.get("supplier") or "").strip()
    f_leaf = (request.args.get("leaf") or "").strip()
    f_q = (request.args.get("q") or "").strip()
    f_img = (request.args.get("img") or "").strip()
    f_newn = (request.args.get("newn") or "").strip()
    try:
        pg = max(1, int(request.args.get("page") or 1))
    except (TypeError, ValueError):
        pg = 1
    per = 60

    where, params = ["store=%s"], [f_store]
    if f_supplier:
        where.append("supplier=%s"); params.append(f_supplier)
    if f_leaf:
        where.append("hd_path=%s"); params.append(f_leaf)
    if f_q:
        where.append("(supplier_sku LIKE %s OR title LIKE %s OR supplier_cat LIKE %s OR hd_path LIKE %s)")
        params += [f"%{f_q}%"] * 4
    if f_img == "y":
        where.append("has_overview_img=1")
    elif f_img == "n":
        where.append("has_overview_img=0")
    if f_newn == "new":
        where.append("is_new=1")
    elif f_newn == "restock":
        where.append("is_restock=1")
    w = " AND ".join(where) + " AND tier='ai'"

    total = int((_query(f"SELECT COUNT(*) n FROM order_system.hd_selection_pool WHERE {w}",
                        tuple(params)) or [{"n": 0}])[0]["n"])
    pages = max(1, (total + per - 1) // per)
    pg = min(pg, pages)
    rows = _query(f"SELECT * FROM order_system.hd_selection_pool WHERE {w} "
                  f"ORDER BY stock DESC LIMIT %s OFFSET %s",
                  tuple(params) + (per, (pg - 1) * per))

    # 擦边池
    m_sup = (request.args.get("msup") or "").strip()
    m_cat = (request.args.get("mcat") or "").strip()
    m_q = (request.args.get("mq") or "").strip()
    try:
        m_pg = max(1, int(request.args.get("mp") or 1))
    except (TypeError, ValueError):
        m_pg = 1
    m_per = 100
    mw, mp_params = ["store=%s", "tier='manual'"], [f_store]
    if m_sup:
        mw.append("supplier=%s"); mp_params.append(m_sup)
    if m_cat:
        mw.append("supplier_cat=%s"); mp_params.append(m_cat)
    if m_q:
        mw.append("(supplier_sku LIKE %s OR title LIKE %s OR hd_path LIKE %s)")
        mp_params += [f"%{m_q}%"] * 3
    mww = " AND ".join(mw)
    manual_total = int((_query(f"SELECT COUNT(*) n FROM order_system.hd_selection_pool WHERE {mww}",
                               tuple(mp_params)) or [{"n": 0}])[0]["n"])
    m_pages = max(1, (manual_total + m_per - 1) // m_per)
    m_pg = min(m_pg, m_pages)
    manual_rows = _query(f"SELECT * FROM order_system.hd_selection_pool WHERE {mww} "
                         f"ORDER BY stock DESC LIMIT %s OFFSET %s",
                         tuple(mp_params) + (m_per, (m_pg - 1) * m_per))
    manual_cats = _query(
        "SELECT supplier, supplier_cat, COUNT(*) n FROM order_system.hd_selection_pool "
        "WHERE store=%s AND tier='manual' GROUP BY supplier, supplier_cat ORDER BY n DESC", (f_store,))
    leaf_options = sorted({r["hd_path"] for r in _query(
        "SELECT hd_path FROM order_system.hd_leaf_category WHERE store=%s AND active=1", (f_store,))} | {
        r["hd_path"] for r in _query(
            "SELECT DISTINCT hd_path FROM order_system.hd_selection_pool WHERE store=%s AND hd_path IS NOT NULL", (f_store,))})

    leaves = [r["hd_path"] for r in _query(
        "SELECT hd_path, COUNT(*) n FROM order_system.hd_selection_pool "
        "WHERE store=%s AND tier='ai' AND hd_path IS NOT NULL GROUP BY hd_path ORDER BY n DESC", (f_store,))]
    counts = {r["supplier"]: int(r["n"]) for r in _query(
        "SELECT supplier, COUNT(*) n FROM order_system.hd_selection_pool "
        "WHERE store=%s AND tier='ai' GROUP BY supplier", (f_store,))}
    imgc = _query("SELECT SUM(has_overview_img=1) y, SUM(has_overview_img=0) n "
                  "FROM order_system.hd_selection_pool WHERE store=%s AND tier='ai'", (f_store,))
    img_stat = imgc[0] if imgc else {"y": 0, "n": 0}
    nnc = _query("SELECT COUNT(*) a, SUM(is_new) nw, SUM(is_restock) rs "
                 "FROM order_system.hd_selection_pool WHERE store=%s AND tier='ai'", (f_store,))
    newn_stat = nnc[0] if nnc else {"a": 0, "nw": 0, "rs": 0}
    built = _query("SELECT MAX(rebuilt_at) t FROM order_system.hd_selection_pool WHERE store=%s", (f_store,))
    push_log = _query("SELECT store, batch_desc, sku_count, costway_n, vevor_n, leaf_summary, pushed_at "
                      "FROM order_system.hd_push_log WHERE store=%s ORDER BY pushed_at DESC LIMIT 50", (f_store,))

    return render_template("hd_selection/page.html", rows=rows, total=total, page=pg, pages=pages, per=per,
                           f_store=f_store, f_supplier=f_supplier, f_leaf=f_leaf, f_q=f_q, f_img=f_img,
                           f_newn=f_newn, newn_stat=newn_stat, leaves=leaves, counts=counts, img_stat=img_stat,
                           built_at=built[0]["t"] if built else None, push_log=push_log,
                           manual_rows=manual_rows, manual_total=manual_total,
                           m_sup=m_sup, m_cat=m_cat, m_q=m_q, m_pg=m_pg, m_pages=m_pages,
                           manual_cats=manual_cats, leaf_options=leaf_options)


@hd_selection_bp.route("/rebuild", methods=["POST"])
def rebuild():
    store = (request.form.get("store") or request.args.get("store") or "top").strip().lower()
    if store not in STORES:
        store = "top"
    if _REBUILD["running"]:
        return jsonify({"success": False, "msg": "正在重建中，请稍候"})
    _REBUILD["running"] = True
    app_obj = current_app._get_current_object()

    def _bg():
        try:
            with app_obj.app_context():
                from app.services.hd_selection_service import rebuild_pool
                print("[hd_selection] rebuild:", rebuild_pool(store))
        except Exception as exc:
            print("[hd_selection] rebuild failed:", exc)
        finally:
            _REBUILD["running"] = False

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({"success": True, "msg": "候选池后台重建中，几分钟后刷新"})


_DEC_UPSERT = (
    "INSERT INTO order_system.hd_selection_decision "
    "(store,supplier,supplier_sku,decision,override_leaf,override_brand) VALUES (%s,%s,%s,%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE decision=VALUES(decision), "
    "override_leaf=COALESCE(VALUES(override_leaf),override_leaf), "
    "override_brand=COALESCE(VALUES(override_brand),override_brand)")


@hd_selection_bp.route("/triage", methods=["POST"])
def triage():
    data = request.get_json(silent=True) or {}
    action = (data.get("action") or "").strip()
    store = (data.get("store") or "top").strip().lower()
    if store not in STORES:
        store = "top"
    try:
        if action == "approve":
            items = data.get("items") or []
            by_id = {}
            for it in items:
                try:
                    by_id[int(it.get("id"))] = (it.get("leaf") or "").strip() or None
                except (TypeError, ValueError):
                    continue
            if not by_id:
                return jsonify({"success": False, "msg": "没有勾选任何产品"})
            ph = ",".join(["%s"] * len(by_id))
            rows = _query(f"SELECT id, supplier, supplier_sku, hd_path FROM order_system.hd_selection_pool "
                          f"WHERE store=%s AND id IN ({ph})", tuple([store] + list(by_id)))
            stmts = []
            for r in rows:
                oleaf = by_id.get(r["id"]) or r["hd_path"]
                stmts.append((_DEC_UPSERT, (store, r["supplier"], r["supplier_sku"], "approved", oleaf, None)))
                stmts.append(("UPDATE order_system.hd_selection_pool SET tier='ai', hd_path=%s WHERE store=%s AND id=%s",
                              (oleaf, store, r["id"])))
            _write(stmts)
            return jsonify({"success": True, "msg": f"已采用 {len(rows)} 个进精选池", "n": len(rows)})

        if action == "reject":
            ids = [int(x) for x in (data.get("ids") or []) if str(x).isdigit()]
            if not ids:
                return jsonify({"success": False, "msg": "没有勾选任何产品"})
            ph = ",".join(["%s"] * len(ids))
            rows = _query(f"SELECT supplier, supplier_sku FROM order_system.hd_selection_pool "
                          f"WHERE store=%s AND id IN ({ph})", tuple([store] + ids))
            stmts = [(_DEC_UPSERT, (store, r["supplier"], r["supplier_sku"], "rejected", None, None)) for r in rows]
            stmts.append((f"DELETE FROM order_system.hd_selection_pool WHERE store=%s AND id IN ({ph})",
                          tuple([store] + ids)))
            _write(stmts)
            return jsonify({"success": True, "msg": f"已弃用 {len(rows)} 个", "n": len(rows)})

        if action == "map_skus":
            supplier = (data.get("supplier") or "").strip()
            leaf = (data.get("leaf") or "").strip()
            skus = [str(x).strip() for x in (data.get("skus") or []) if str(x).strip()]
            if not (supplier and leaf and skus):
                return jsonify({"success": False, "msg": "缺供应商/目标类目/勾选产品"})
            stmts = [(_DEC_UPSERT, (store, supplier, sku[:64], "approved", leaf, None)) for sku in skus]
            _write(stmts)
            return jsonify({"success": True, "n": len(skus),
                            "msg": f"已把勾选的 {len(skus)} 个映射到 {leaf.split('/')[-1]}（重建后进精选，不含将来新品）"})

        if action == "apply_category":
            supplier = (data.get("supplier") or "").strip()
            supplier_cat = (data.get("supplier_cat") or "").strip()
            leaf = (data.get("leaf") or "").strip()
            if not (supplier and supplier_cat and leaf):
                return jsonify({"success": False, "msg": "缺供应商/供应商类目/目标类目"})
            stmts = [
                ("INSERT INTO order_system.hd_cat_override (store,supplier,supplier_cat,override_leaf) "
                 "VALUES (%s,%s,%s,%s) ON DUPLICATE KEY UPDATE override_leaf=VALUES(override_leaf)",
                 (store, supplier, supplier_cat, leaf)),
                ("UPDATE order_system.hd_selection_pool SET tier='ai', hd_path=%s "
                 "WHERE store=%s AND supplier=%s AND supplier_cat=%s", (leaf, store, supplier, supplier_cat)),
            ]
            _write(stmts)
            return jsonify({"success": True,
                            "msg": f"已把「{supplier_cat[:40]}」整类套用为 {leaf.split('/')[-1]} 并记住(含将来新品)"})

        return jsonify({"success": False, "msg": "未知操作"}), 400
    except Exception as exc:
        return jsonify({"success": False, "msg": str(exc)[:200]}), 500


@hd_selection_bp.route("/unmapped")
def unmapped():
    store = _store()
    leaf_options = sorted({r["hd_path"] for r in _query(
        "SELECT hd_path FROM order_system.hd_leaf_category WHERE store=%s AND active=1", (store,))})
    f_sup = (request.args.get("supplier") or "").strip()
    f_cat = (request.args.get("cat") or "").strip()

    if f_sup == "Costway" and f_cat:
        products = _query("""SELECT d.SKU AS sku, c.title, c.image_url AS img, d.Stock AS stock, d.Price AS price
                             FROM autooperate.newestdropship d
                             JOIN order_system.safety_product_cache c ON c.sku=d.SKU AND c.supplier='Costway'
                             WHERE COALESCE(d.`status`,'Enabled')<>'Disabled' AND c.category=%s AND d.Stock>50
                               AND NOT EXISTS(SELECT 1 FROM order_system.hd_used_sku uu WHERE uu.store=%s AND uu.supplier_sku=d.SKU)
                             ORDER BY d.Stock DESC LIMIT 300""", (f_cat, store))
        return render_template("hd_selection/unmapped.html", store=store, rows=None, products=products,
                               leaf_options=leaf_options, total_n=0, f_sup=f_sup, f_cat=f_cat)
    if f_sup == "Vevor" and f_cat:
        products = _query("""SELECT v.sku, v.title, v.image AS img, v.inventory AS stock, v.price
                             FROM autooperate.vevor_feed v
                             WHERE TRIM(v.product_type)=%s AND v.inventory>50
                               AND NOT EXISTS(SELECT 1 FROM order_system.hd_used_sku uu WHERE uu.store=%s AND uu.supplier_sku=v.sku)
                             ORDER BY v.inventory DESC LIMIT 300""", (f_cat, store))
        return render_template("hd_selection/unmapped.html", store=store, rows=None, products=products,
                               leaf_options=leaf_options, total_n=0, f_sup=f_sup, f_cat=f_cat)

    rows = _query("""
        SELECT supplier, cat, n, img FROM (
          SELECT 'Costway' AS supplier, c.category AS cat, COUNT(*) AS n, MAX(c.image_url) AS img
          FROM autooperate.newestdropship d
          JOIN order_system.safety_product_cache c ON c.sku=d.SKU AND c.supplier='Costway'
          WHERE COALESCE(d.`status`,'Enabled')<>'Disabled' AND c.category<>'' AND d.Stock>50
            AND NOT EXISTS(SELECT 1 FROM order_system.hd_cat_map m WHERE m.store=%s AND m.supplier='Costway' AND m.supplier_cat=c.category AND m.hd_path IS NOT NULL)
            AND NOT EXISTS(SELECT 1 FROM order_system.hd_cat_override o WHERE o.store=%s AND o.supplier='Costway' AND o.supplier_cat=c.category)
            AND NOT EXISTS(SELECT 1 FROM order_system.hd_used_sku uu WHERE uu.store=%s AND uu.supplier_sku=d.SKU)
          GROUP BY c.category
          UNION ALL
          SELECT 'Vevor' AS supplier, TRIM(v.product_type) AS cat, COUNT(*) AS n, MAX(v.image) AS img
          FROM autooperate.vevor_feed v
          WHERE v.product_type<>'' AND v.inventory>50
            AND NOT EXISTS(SELECT 1 FROM order_system.hd_cat_map m WHERE m.store=%s AND m.supplier='Vevor' AND m.supplier_cat=TRIM(v.product_type) AND m.hd_path IS NOT NULL)
            AND NOT EXISTS(SELECT 1 FROM order_system.hd_cat_override o WHERE o.store=%s AND o.supplier='Vevor' AND o.supplier_cat=TRIM(v.product_type))
            AND NOT EXISTS(SELECT 1 FROM order_system.hd_used_sku uu WHERE uu.store=%s AND uu.supplier_sku=v.sku)
          GROUP BY TRIM(v.product_type)
        ) t ORDER BY n DESC LIMIT 500""", (store, store, store, store, store, store))
    total_n = sum(int(r["n"]) for r in rows)
    return render_template("hd_selection/unmapped.html", store=store, rows=rows, products=None,
                           leaf_options=leaf_options, total_n=total_n, f_sup="", f_cat="")


@hd_selection_bp.route("/push", methods=["POST"])
def push():
    from app.services.hd_selection_service import push_to_feishu
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
