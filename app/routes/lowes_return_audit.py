# -*- coding: utf-8 -*-
"""Lowes-Autool 退货运费稽核页面（/lowes-return-audit）。"""
import json

from flask import Blueprint, jsonify, render_template, request

from app.models.db_manager import DBManager

lowes_return_audit_bp = Blueprint("lowes_return_audit", __name__)


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


@lowes_return_audit_bp.route("/")
def page():
    f_match = (request.args.get("m") or "").strip()   # tracking/po/inferred/none/''
    where, params = ["1=1"], []
    if f_match:
        where.append("match_type=%s")
        params.append(f_match)
    w = " AND ".join(where)

    rows = _query(f"""SELECT * FROM order_system.fedex_return_audit
                      WHERE {w} ORDER BY net_charge DESC""", tuple(params))
    for r in rows:
        r["candidates"] = json.loads(r["candidates_json"]) if r.get("candidates_json") else []

    stat = _query("""SELECT
        COUNT(*) n, COALESCE(SUM(net_charge),0) ship_total,
        COALESCE(SUM(CASE WHEN order_id IS NOT NULL THEN net_charge END),0) ship_matched,
        COALESCE(SUM(CASE WHEN claim_filed=1 THEN cost END),0) claim_cost,
        COALESCE(SUM(CASE WHEN claim_filed=0 THEN cost END),0) unclaim_cost,
        COALESCE(SUM(CASE WHEN order_id IS NOT NULL THEN cost END),0) cost_matched,
        SUM(order_id IS NOT NULL) matched_n,
        SUM(match_type='tracking') n_track, SUM(match_type='po') n_po,
        SUM(match_type='inferred') n_infer, SUM(match_type='manual') n_manual,
        SUM(match_type='none') n_none
        FROM order_system.fedex_return_audit""")
    s = stat[0] if stat else {}
    total = int(s.get("n") or 0)
    matched_n = int(s.get("matched_n") or 0)
    s["match_rate"] = round(matched_n * 100.0 / total, 1) if total else 0.0
    return render_template("lowes_return_audit/page.html", rows=rows, s=s,
                           total=total, f_match=f_match)


@lowes_return_audit_bp.route("/upload", methods=["POST"])
def upload():
    from app.services.lowes_return_audit_service import ingest_and_match
    f = request.files.get("invoice")
    if not f or not f.filename:
        return jsonify({"success": False, "msg": "没选文件"})
    try:
        res = ingest_and_match(f.read(), f.filename)
        return jsonify({"success": True, **res})
    except Exception as exc:
        return jsonify({"success": False, "msg": str(exc)[:300]}), 500


@lowes_return_audit_bp.route("/rematch", methods=["POST"])
def rematch():
    from app.services.lowes_return_audit_service import rematch_all
    try:
        return jsonify({"success": True, **rematch_all(only_unconfirmed=True)})
    except Exception as exc:
        return jsonify({"success": False, "msg": str(exc)[:300]}), 500


@lowes_return_audit_bp.route("/confirm", methods=["POST"])
def confirm():
    from app.services.lowes_return_audit_service import confirm_match
    data = request.get_json(silent=True) or {}
    tracking = (data.get("tracking") or "").strip()
    order_id = (data.get("order_id") or "").strip()
    if not tracking or not order_id:
        return jsonify({"success": False, "msg": "缺参数"})
    try:
        return jsonify(confirm_match(tracking, order_id))
    except Exception as exc:
        return jsonify({"success": False, "msg": str(exc)[:300]}), 500


@lowes_return_audit_bp.route("/unbind", methods=["POST"])
def unbind_route():
    from app.services.lowes_return_audit_service import unbind
    data = request.get_json(silent=True) or {}
    tracking = (data.get("tracking") or "").strip()
    if not tracking:
        return jsonify({"success": False, "msg": "缺参数"})
    try:
        return jsonify(unbind(tracking))
    except Exception as exc:
        return jsonify({"success": False, "msg": str(exc)[:300]}), 500
