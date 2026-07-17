# -*- coding: utf-8 -*-
"""AI助理接口（右下角对话框）。登录门卫走全局 before_request。"""
from flask import Blueprint, jsonify, request

assistant_bp = Blueprint("assistant", __name__)


@assistant_bp.route("/chat", methods=["POST"])
def chat():
    from app.services.assistant_service import chat as do_chat
    payload = request.get_json(silent=True) or {}
    try:
        result = do_chat(
            question=payload.get("message") or "",
            history=payload.get("history") or [],
            page=payload.get("page") or "",
        )
    except Exception as exc:
        return jsonify({"success": False,
                        "msg": f"助理暂时不可用：{str(exc)[:200]}"}), 500
    status = 200 if result.get("success") else 400
    return jsonify(result), status
