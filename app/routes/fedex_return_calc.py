# -*- coding: utf-8 -*-
"""FedEx 退货运费测算页面(/fedex-return-calc)。只给始发ZIP+尺寸+重量，自动查zone算费。"""
from flask import Blueprint, jsonify, render_template, request

fedex_return_calc_bp = Blueprint("fedex_return_calc", __name__)


@fedex_return_calc_bp.route("/")
def page():
    from app.services.fedex_return_service import (
        get_fuel_rate, DEST_ZIP, DEFAULT_DISCOUNT,
    )
    fuel, updated = get_fuel_rate()
    return render_template(
        "fedex_return_calc/page.html",
        dest_zip=DEST_ZIP, fuel_rate=str(fuel),
        fuel_updated=updated or "—", discount=str(DEFAULT_DISCOUNT),
    )


@fedex_return_calc_bp.route("/estimate", methods=["POST"])
def estimate():
    from app.services.fedex_return_service import estimate as do_estimate
    d = request.get_json(silent=True) or {}
    zip_ = (str(d.get("origin_zip") or "")).strip()
    if not zip_:
        return jsonify({"ok": False, "msg": "请填始发ZIP"}), 400
    try:
        L, W, H = float(d.get("length")), float(d.get("width")), float(d.get("height"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "msg": "长/宽/高必须都填数字(英寸)"}), 400
    if min(L, W, H) <= 0:
        return jsonify({"ok": False, "msg": "长/宽/高必须>0"}), 400
    aw = d.get("actual_weight")
    try:
        aw = float(aw) if aw not in (None, "", "None") else None
    except (TypeError, ValueError):
        return jsonify({"ok": False, "msg": "实际重量必须是数字(磅)或留空"}), 400
    try:
        res = do_estimate(
            origin_zip=zip_, length=L, width=W, height=H, actual_weight=aw,
            return_method=(d.get("return_method") or "none"),
            residential=bool(d.get("residential")),
            signature=(d.get("signature") or "none"),
            packaging_ahs=bool(d.get("packaging_ahs")),
            declared_value=d.get("declared_value"),
            billing=("third-party" if d.get("third_party") else "sender"),
        )
        return jsonify(res)
    except Exception as exc:
        return jsonify({"ok": False, "msg": str(exc)[:300]}), 500
