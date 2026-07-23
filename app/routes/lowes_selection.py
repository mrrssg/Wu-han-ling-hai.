# -*- coding: utf-8 -*-
"""Lowes 选品候选池页面（/lowes-selection）。Autool=豪雅 / Yasonic=司顺，单店重建。"""
import threading
from flask import Blueprint, current_app, jsonify, render_template, request

from app.models.db_manager import DBManager

lowes_selection_bp = Blueprint("lowes_selection", __name__)

_REBUILD = {"autool": False, "yasonic": False}
STORES = {"autool": "Lowes-Autool（豪雅）", "yasonic": "Lowes-Yasonic（司顺）"}


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
    built = _query("SELECT MAX(rebuilt_at) t FROM order_system.lowes_selection_pool WHERE store=%s",
                   (store,))
    push_log = _query("""SELECT batch_desc, sku_count, leaf_summary, pushed_at
                         FROM order_system.lowes_push_log WHERE store=%s
                         ORDER BY pushed_at DESC LIMIT 50""", (store,))
    store_totals = {r["store"]: int(r["n"]) for r in _query(
        "SELECT store, COUNT(*) n FROM order_system.lowes_selection_pool GROUP BY store")}
    return render_template("lowes_selection/page.html", rows=rows, total=total,
                           page=pg, pages=pages, per=per, store=store, stores=STORES,
                           f_leaf=f_leaf, f_q=f_q, f_img=f_img,
                           leaves=leaves, img_stat=img_stat,
                           built_at=built[0]["t"] if built else None,
                           push_log=push_log, store_totals=store_totals,
                           rebuilding=_REBUILD.get(store, False))


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
