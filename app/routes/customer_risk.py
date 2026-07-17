# -*- coding: utf-8 -*-
"""可疑客户分析页面（/customer-risk）。"""
import threading
from flask import Blueprint, current_app, jsonify, render_template, request

from app.models.db_manager import DBManager

customer_risk_bp = Blueprint("customer_risk", __name__)


def _query(sql, params=None):
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params) if params else cur.execute(sql)
            return cur.fetchall() or []
    finally:
        conn.close()


@customer_risk_bp.route("/")
def page():
    level = (request.args.get("level") or "").strip()
    where = "WHERE risk_level=%s" if level in ("high", "mid", "low") else ""
    params = (level,) if where else None
    rows = _query(f"""
        SELECT * FROM order_system.customer_risk_profile {where}
        ORDER BY FIELD(risk_level,'high','mid','low'), return_rate DESC, returns_n DESC
        LIMIT 500""", params)
    counts = {r["risk_level"]: int(r["n"]) for r in _query(
        """SELECT risk_level, COUNT(*) AS n FROM order_system.customer_risk_profile
           GROUP BY risk_level""")}
    built = _query("SELECT MAX(built_at) AS t FROM order_system.customer_risk_profile")
    return render_template("customer_risk/page.html", rows=rows, counts=counts,
                           level=level,
                           built_at=built[0]["t"] if built else None)


@customer_risk_bp.route("/rebuild", methods=["POST"])
def rebuild():
    app_obj = current_app._get_current_object()

    def _bg():
        with app_obj.app_context():
            from app.services.customer_risk_service import rebuild_profiles
            try:
                print("[customer_risk] rebuild:", rebuild_profiles())
            except Exception as exc:
                print("[customer_risk] rebuild failed:", exc)

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({"success": True, "msg": "后台重算已开始，几分钟后刷新本页"})


@customer_risk_bp.route("/blacklist/<int:pid>", methods=["POST"])
def to_blacklist(pid):
    from app.services.customer_risk_service import add_to_blacklist
    return jsonify(add_to_blacklist(pid))
