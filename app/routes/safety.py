# -*- coding: utf-8 -*-
"""产品安全防控页面（/safety）。"""
import os
import time
from flask import (Blueprint, flash, jsonify, redirect, render_template,
                   request, send_from_directory, url_for)

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
    from app.services.safety_service import CASE_TYPES
    cases = _query("""
        SELECT c.*,
               (SELECT COUNT(*) FROM order_system.safety_hit h
                 WHERE h.case_id=c.id) AS hits_n,
               (SELECT COUNT(*) FROM order_system.safety_hit h
                 WHERE h.case_id=c.id AND h.active=1 AND h.status='open') AS selling_open
        FROM order_system.safety_case c ORDER BY c.id DESC LIMIT 200""")
    return render_template("safety/cases.html", cases=cases, case_types=CASE_TYPES)


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
        SELECT * FROM order_system.safety_hit WHERE case_id=%s
        ORDER BY (active=1) DESC, status='open' DESC, orders_90d DESC, shop_sku""", (cid,))
    import json as _json
    files = []
    try:
        files = _json.loads(case[0].get("files_json") or "[]")
    except ValueError:
        pass
    return render_template("safety/case_detail.html", case=case[0], hits=hits, files=files)


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
