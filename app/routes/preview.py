"""新版界面预览（已转正）：样本定稿后各页面已直接使用 base_v2，
此蓝图仅保留跳转，避免旧链接404。"""
from flask import Blueprint, redirect, url_for

preview_bp = Blueprint("preview", __name__)


@preview_bp.route("/")
def home():
    return redirect(url_for("main.index"))


@preview_bp.route("/orders", methods=["GET", "POST"])
def orders():
    return redirect(url_for("orders.search_orders"))
