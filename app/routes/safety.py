# -*- coding: utf-8 -*-
"""产品安全防控页面（/safety）。"""
import json
import os
import threading
import time
from flask import (Blueprint, current_app, flash, jsonify, redirect,
                   render_template, request, send_from_directory, url_for)

from app.models.db_manager import DBManager

safety_bp = Blueprint("safety", __name__)


def _query(sql, params=None):
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params) if params else cur.execute(sql)
            return cur.fetchall() or []
    finally:
        conn.close()


@safety_bp.route("/")
def cases_page():
    from app.services.safety_service import CASE_TYPES, cache_stats
    cases = _query("""
        SELECT c.*,
               (SELECT COUNT(*) FROM order_system.safety_hit h
                 WHERE h.case_id=c.id) AS hits_n,
               (SELECT COUNT(*) FROM order_system.safety_hit h
                 WHERE h.case_id=c.id AND h.active=1 AND h.status='open') AS selling_open
        FROM order_system.safety_case c ORDER BY c.id DESC LIMIT 200""")
    return render_template("safety/cases.html", cases=cases, case_types=CASE_TYPES,
                           cache=cache_stats())


@safety_bp.route("/cache/sync", methods=["POST"])
def cache_sync():
    """后台全量刷新产品文本缓存（飞书供应商资料→DB，约几分钟）。"""
    app_obj = current_app._get_current_object()

    def _bg():
        with app_obj.app_context():
            from app.services.safety_service import sync_product_cache
            try:
                print("[safety] cache sync:", sync_product_cache())
            except Exception as exc:
                print("[safety] cache sync failed:", exc)

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({"success": True, "msg": "后台同步已开始，几分钟后刷新本页看条数"})


@safety_bp.route("/case/<int:cid>/fingerprint", methods=["POST"])
def fingerprint_gen(cid):
    from app.services.safety_service import generate_fingerprint
    try:
        return jsonify(generate_fingerprint(cid))
    except Exception as exc:
        return jsonify({"success": False, "msg": str(exc)[:300]}), 500


@safety_bp.route("/case/<int:cid>/fingerprint/save", methods=["POST"])
def fingerprint_save(cid):
    from app.services.safety_service import save_fingerprint
    fp_text = (request.get_json(silent=True) or {}).get("fingerprint") or ""
    return jsonify(save_fingerprint(cid, fp_text))


@safety_bp.route("/case/<int:cid>/scan", methods=["POST"])
def scan_start(cid):
    """后台跑全库举一反三扫描（百来个候选逐个AI精判，约5~15分钟）。"""
    app_obj = current_app._get_current_object()

    def _bg():
        with app_obj.app_context():
            from app.services.safety_service import run_scan
            from app.models.db_manager import DBManager as _DBM
            try:
                print(f"[safety] scan case {cid}:", run_scan(cid))
            except Exception as exc:
                print(f"[safety] scan case {cid} failed:", exc)
                conn = _DBM.get_connection()
                try:
                    with conn.cursor() as cur:
                        cur.execute("""UPDATE order_system.safety_case
                                       SET scan_status='error' WHERE id=%s""", (cid,))
                    conn.commit()
                finally:
                    conn.close()

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({"success": True, "msg": "扫描已在后台开始，几分钟后刷新本页看结果"})


@safety_bp.route("/new", methods=["POST"])
def new_case():
    from app.services.safety_service import ALLOWED_EXT, create_case, files_root
    case_type = (request.form.get("case_type") or "其它").strip()
    title = (request.form.get("title") or "").strip()
    supplier = (request.form.get("supplier") or "Costway").strip()
    case_text = (request.form.get("case_text") or "").strip()
    skus_text = request.form.get("skus") or ""
    if not title:
        flash("案例标题必填", "danger")
        return redirect(url_for("safety.cases_page"))

    saved = []
    folder = os.path.join(files_root(), time.strftime("%Y%m%d%H%M%S"))
    for f in request.files.getlist("files"):
        if not f or not f.filename:
            continue
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ALLOWED_EXT:
            flash(f"跳过不支持的文件类型：{f.filename}", "warning")
            continue
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f.filename)
        f.save(path)
        saved.append({"name": f.filename, "path": path})

    result = create_case(case_type, title, supplier, case_text, skus_text, saved)
    msg = (f"案例#{result['case_id']}已建：有效供应商SKU {result['skus_valid']}个，"
           f"同款家族共{result['family_size']}个，命中店铺SKU {result['hits']}个"
           f"（其中在卖 {result['selling']} 个已上首页红条）")
    if result["skus_invalid"]:
        msg += f"；这些SKU在供应商/映射数据里找不到，请核对：{', '.join(result['skus_invalid'][:10])}"
    flash(msg, "success" if not result["skus_invalid"] else "warning")
    return redirect(url_for("safety.case_detail", cid=result["case_id"]))


@safety_bp.route("/case/<int:cid>")
def case_detail(cid):
    case = _query("SELECT * FROM order_system.safety_case WHERE id=%s", (cid,))
    if not case:
        flash("案例不存在", "danger")
        return redirect(url_for("safety.cases_page"))
    hits = _query("""
        SELECT h.*, c.image_url AS product_img, c.title AS product_title
        FROM order_system.safety_hit h
        LEFT JOIN (SELECT sku, MAX(image_url) AS image_url, MAX(title) AS title
                   FROM order_system.safety_product_cache GROUP BY sku) c
          ON c.sku = h.supplier_sku
        WHERE h.case_id=%s
        ORDER BY (h.active=1) DESC, h.status='open' DESC, h.orders_90d DESC, h.shop_sku""",
        (cid,))
    import json as _json
    files = []
    try:
        files = _json.loads(case[0].get("files_json") or "[]")
    except ValueError:
        pass
    for f in files:
        f["is_img"] = (f.get("name") or "").lower().endswith(
            (".png", ".jpg", ".jpeg", ".webp"))
    # 涉事SKU的供应商主图（和命中清单缩略图并排对比用）
    involved = []
    skus = [s for s in (case[0].get("supplier_skus") or "").split(",") if s][:12]
    if skus:
        ph = ",".join(["%s"] * len(skus))
        img_map = {r["sku"]: r for r in _query(
            f"""SELECT sku, MAX(image_url) AS img, MAX(title) AS title
                FROM order_system.safety_product_cache
                WHERE sku IN ({ph}) GROUP BY sku""", tuple(skus))}
        for s in skus:
            r = img_map.get(s)
            involved.append({"sku": s, "img": (r or {}).get("img"),
                             "title": (r or {}).get("title") or ""})
    return render_template("safety/case_detail.html", case=case[0], hits=hits,
                           files=files, involved=involved)


@safety_bp.route("/file/<path:relpath>")
def case_file(relpath):
    from app.services.safety_service import files_root
    return send_from_directory(files_root(), relpath)


@safety_bp.route("/hit/<int:hid>/mark", methods=["POST"])
def hit_mark(hid):
    from app.services.safety_service import mark_hit
    status = (request.get_json(silent=True) or {}).get("status") or ""
    ok = mark_hit(hid, status)
    return jsonify({"success": ok})


@safety_bp.route("/case/<int:cid>/close", methods=["POST"])
def case_close(cid):
    from app.services.safety_service import close_case
    close_case(cid)
    return jsonify({"success": True})
