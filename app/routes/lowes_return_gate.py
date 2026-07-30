# -*- coding: utf-8 -*-
"""Lowes-Autool 退货把关页面(/lowes-return-gate)。货值 vs 退货运费 → 退不退建议。"""
from collections import Counter

from flask import Blueprint, current_app, jsonify, render_template, request

lowes_return_gate_bp = Blueprint("lowes_return_gate", __name__)


@lowes_return_gate_bp.route("/")
def page():
    from app.services.lowes_return_gate_service import list_pending
    rows = list_pending(limit=200)
    counts = Counter(r["verdict"] for r in rows)
    return render_template("lowes_return_gate/page.html",
                           rows=rows, counts=counts, total=len(rows))


@lowes_return_gate_bp.route("/ai-estimate", methods=["POST"])
def ai_estimate():
    from app.services.lowes_return_gate_service import list_pending, ai_estimate_skus
    rows = list_pending(limit=200)
    need = [{"sku": r["sku"], "warehouse_sku": r["warehouse_sku"], "category": r["category"]}
            for r in rows if r["verdict"] == "need_ai" and r["sku"]]
    if not need:
        return jsonify({"success": True, "estimated": 0, "msg": "没有待AI估算的退货"})
    try:
        n = ai_estimate_skus(current_app.config["BASE_DIR"], need)
        return jsonify({"success": True, "estimated": n, "candidates": len(need)})
    except Exception as exc:
        return jsonify({"success": False, "msg": str(exc)[:300]}), 500


@lowes_return_gate_bp.route("/recompute", methods=["POST"])
def recompute_route():
    from app.services.lowes_return_gate_service import recompute
    d = request.get_json(silent=True) or {}
    try:
        L, W, H = float(d.get("L")), float(d.get("W")), float(d.get("H"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "msg": "长宽高要填数字"}), 400
    try:
        res = recompute((d.get("sku") or "").strip(), (d.get("zip") or "").strip(),
                        L, W, H, d.get("wt"))
        return jsonify(res)
    except Exception as exc:
        return jsonify({"ok": False, "msg": str(exc)[:300]}), 500
