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
    # 🖐人工待选(擦边)池:tier='manual',有自己的筛选(msup供应商/mcat供应商类目/mq搜索)+分页(mp)
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
        mw.append("(supplier_sku LIKE %s OR title LIKE %s OR macy_leaf LIKE %s)")
        mp_params += [f"%{m_q}%"] * 3
    mww = " AND ".join(mw)
    manual_total = int((_query(f"SELECT COUNT(*) n FROM order_system.macy_selection_pool WHERE {mww}",
                               tuple(mp_params)) or [{"n": 0}])[0]["n"])
    m_pages = max(1, (manual_total + m_per - 1) // m_per)
    m_pg = min(m_pg, m_pages)
    manual_rows = _query(f"SELECT * FROM order_system.macy_selection_pool WHERE {mww} "
                         f"ORDER BY heat_90d DESC, stock DESC LIMIT %s OFFSET %s",
                         tuple(mp_params) + (m_per, (m_pg - 1) * m_per))
    # 擦边池里各供应商类目 + 数量(供快速筛选下拉)
    manual_cats = _query(
        "SELECT supplier, supplier_cat, COUNT(*) n FROM order_system.macy_selection_pool "
        "WHERE store=%s AND tier='manual' GROUP BY supplier, supplier_cat ORDER BY n DESC", (f_store,))
    # 可改成的 Macy 叶子清单(该店有效叶子:官方叶子表 ∪ 该店映射过的叶子)
    leaf_options = sorted({r["leaf"] for r in _query(
        "SELECT leaf FROM order_system.macy_leaf_category WHERE active=1")} | {
        r["macy_leaf"] for r in _query(
            "SELECT DISTINCT macy_leaf FROM order_system.macy_selection_pool "
            "WHERE store=%s AND macy_leaf IS NOT NULL", (f_store,))})
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
                           f_store=f_store, manual_rows=manual_rows, manual_total=manual_total,
                           m_sup=m_sup, m_cat=m_cat, m_q=m_q, m_pg=m_pg, m_pages=m_pages,
                           manual_cats=manual_cats, leaf_options=leaf_options)


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


def _write(statements):
    """一个连接内顺序执行写语句并 commit;返回受影响行数合计。statements=[(sql,params),...]"""
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


_DEC_UPSERT = (
    "INSERT INTO order_system.macy_selection_decision "
    "(store,supplier,supplier_sku,decision,override_leaf,override_brand) "
    "VALUES (%s,%s,%s,%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE decision=VALUES(decision), "
    "override_leaf=COALESCE(VALUES(override_leaf),override_leaf), "
    "override_brand=COALESCE(VALUES(override_brand),override_brand)")


@macy_selection_bp.route("/triage", methods=["POST"])
def triage():
    """擦边池人工操作：采用进精选(可改类目)/弃用/按供应商类目套用+记住映射。"""
    data = request.get_json(silent=True) or {}
    action = (data.get("action") or "").strip()
    store = (data.get("store") or "kuyotq").strip().lower()
    if store not in ("kuyotq", "wopet"):
        store = "kuyotq"
    try:
        if action == "approve":
            # items=[{id, leaf?}] 逐行(可各带改后类目);无 leaf 用行原类目
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
            rows = _query(f"SELECT id, supplier, supplier_sku, macy_leaf "
                          f"FROM order_system.macy_selection_pool WHERE store=%s AND id IN ({ph})",
                          tuple([store] + list(by_id)))
            stmts = []
            for r in rows:
                oleaf = by_id.get(r["id"]) or r["macy_leaf"]
                stmts.append((_DEC_UPSERT,
                              (store, r["supplier"], r["supplier_sku"], "approved", oleaf, None)))
                stmts.append(("UPDATE order_system.macy_selection_pool "
                              "SET tier='ai', macy_leaf=%s WHERE store=%s AND id=%s",
                              (oleaf, store, r["id"])))
            _write(stmts)
            return jsonify({"success": True, "msg": f"已采用 {len(rows)} 个进精选池", "n": len(rows)})

        if action == "reject":
            ids = [int(x) for x in (data.get("ids") or []) if str(x).isdigit()]
            if not ids:
                return jsonify({"success": False, "msg": "没有勾选任何产品"})
            ph = ",".join(["%s"] * len(ids))
            rows = _query(f"SELECT supplier, supplier_sku FROM order_system.macy_selection_pool "
                          f"WHERE store=%s AND id IN ({ph})", tuple([store] + ids))
            stmts = [(_DEC_UPSERT, (store, r["supplier"], r["supplier_sku"], "rejected", None, None))
                     for r in rows]
            stmts.append((f"DELETE FROM order_system.macy_selection_pool "
                          f"WHERE store=%s AND id IN ({ph})", tuple([store] + ids)))
            _write(stmts)
            return jsonify({"success": True, "msg": f"已弃用 {len(rows)} 个", "n": len(rows)})

        if action == "apply_category":
            supplier = (data.get("supplier") or "").strip()
            supplier_cat = (data.get("supplier_cat") or "").strip()
            leaf = (data.get("leaf") or "").strip()
            brand = (data.get("brand") or "").strip() or None
            if not (supplier and supplier_cat and leaf):
                return jsonify({"success": False, "msg": "缺供应商/供应商类目/目标类目"})
            if not brand:   # 没给品牌→按目标叶子在 macy_leaf_category 里取(kuyotq按叶子,wopet固定)
                if store == "wopet":
                    brand = "COZITO"
                else:
                    lb = _query("SELECT brand FROM order_system.macy_leaf_category "
                                "WHERE active=1 AND leaf=%s AND brand IS NOT NULL LIMIT 1", (leaf,))
                    brand = lb[0]["brand"] if lb else None
            stmts = [
                ("INSERT INTO order_system.macy_cat_override "
                 "(store,supplier,supplier_cat,override_leaf,override_brand) VALUES (%s,%s,%s,%s,%s) "
                 "ON DUPLICATE KEY UPDATE override_leaf=VALUES(override_leaf), "
                 "override_brand=VALUES(override_brand)",
                 (store, supplier, supplier_cat, leaf, brand)),
                ("UPDATE order_system.macy_selection_pool SET tier='ai', macy_leaf=%s "
                 "WHERE store=%s AND supplier=%s AND supplier_cat=%s",
                 (leaf, store, supplier, supplier_cat)),
            ]
            n = _write(stmts)
            return jsonify({"success": True,
                            "msg": f"已把「{supplier_cat[:40]}」整类套用为 {leaf} 并记住(含将来新品)"})

        return jsonify({"success": False, "msg": "未知操作"}), 400
    except Exception as exc:
        return jsonify({"success": False, "msg": str(exc)[:200]}), 500


@macy_selection_bp.route("/unmapped")
def unmapped():
    """🕳 未归类复核桶:有货>50 但供应商类目没归到任何 Macy 叶子(prefilter/AI漏掉/feed新类目)
    → 不在任何池里,彻底隐形。列出来供人工映射(写 macy_cat_override,重建后进池)。"""
    store = (request.args.get("store") or "kuyotq").strip().lower()
    if store not in ("kuyotq", "wopet"):
        store = "kuyotq"
    leaf_options = sorted({r["leaf"] for r in _query(
        "SELECT leaf FROM order_system.macy_leaf_category WHERE active=1 AND leaf IS NOT NULL")})
    if store == "wopet":
        # wopet 用逐产品分类器,没有"未映射类目"概念(非宠物是故意排除)
        return render_template("macy_selection/unmapped.html", store=store, rows=[], products=None,
                               is_wopet=True, leaf_options=[], total_n=0, f_sup="", f_cat="")

    f_sup = (request.args.get("supplier") or "").strip()
    f_cat = (request.args.get("cat") or "").strip()

    # 模式二:点了某个类目 → 显示该类目的具体产品(图片+标题+库存+价)
    if f_sup == "Costway" and f_cat:
        products = _query("""SELECT d.SKU AS sku, c.title, c.image_url AS img, d.Stock AS stock, d.Price AS price
                             FROM autooperate.newestdropship d
                             JOIN order_system.safety_product_cache c ON c.sku=d.SKU AND c.supplier='Costway'
                             WHERE COALESCE(d.`status`,'Enabled')<>'Disabled' AND c.category=%s AND d.Stock>50
                             ORDER BY d.Stock DESC LIMIT 300""", (f_cat,))
        return render_template("macy_selection/unmapped.html", store=store, rows=None, products=products,
                               is_wopet=False, leaf_options=leaf_options, total_n=0, f_sup=f_sup, f_cat=f_cat)
    if f_sup == "Vevor" and f_cat:
        products = _query("""SELECT v.sku, v.title, v.image AS img, v.inventory AS stock, v.price
                             FROM autooperate.vevor_feed v
                             WHERE v.product_type=%s AND v.inventory>50
                             ORDER BY v.inventory DESC LIMIT 300""", (f_cat,))
        return render_template("macy_selection/unmapped.html", store=store, rows=None, products=products,
                               is_wopet=False, leaf_options=leaf_options, total_n=0, f_sup=f_sup, f_cat=f_cat)

    # 模式一:类目汇总(带每类目一张样图,好认)
    rows = _query("""
        SELECT supplier, cat, n, img FROM (
          SELECT 'Costway' AS supplier, c.category AS cat, COUNT(*) AS n, MAX(c.image_url) AS img
          FROM autooperate.newestdropship d
          JOIN order_system.safety_product_cache c ON c.sku=d.SKU AND c.supplier='Costway'
          WHERE COALESCE(d.`status`,'Enabled')<>'Disabled' AND c.category<>'' AND d.Stock>50
            AND NOT EXISTS(SELECT 1 FROM order_system.macy_cat_map m
                           WHERE m.supplier='Costway' AND m.supplier_cat=c.category AND m.macy_leaf IS NOT NULL)
            AND NOT EXISTS(SELECT 1 FROM order_system.macy_cat_override o
                           WHERE o.store=%s AND o.supplier='Costway' AND o.supplier_cat=c.category)
          GROUP BY c.category
          UNION ALL
          SELECT 'Vevor' AS supplier, v.product_type AS cat, COUNT(*) AS n, MAX(v.image) AS img
          FROM autooperate.vevor_feed v
          WHERE v.product_type<>'' AND v.inventory>50
            AND NOT EXISTS(SELECT 1 FROM order_system.macy_cat_map m
                           WHERE m.supplier='Vevor' AND m.supplier_cat=v.product_type AND m.macy_leaf IS NOT NULL)
            AND NOT EXISTS(SELECT 1 FROM order_system.macy_cat_override o
                           WHERE o.store=%s AND o.supplier='Vevor' AND o.supplier_cat=v.product_type)
          GROUP BY v.product_type
        ) t ORDER BY n DESC LIMIT 500""", (store, store))
    total_n = sum(int(r["n"]) for r in rows)
    return render_template("macy_selection/unmapped.html", store=store, rows=rows, products=None,
                           is_wopet=False, leaf_options=leaf_options, total_n=total_n, f_sup="", f_cat="")


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
