from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from werkzeug.security import check_password_hash
from app.models.db_manager import DBManager

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["GET"])
def login():
    return render_template("login.html")


@auth_bp.route("/login", methods=["POST"])
def do_login():
    username = (request.form.get("username") or "").strip()
    password = (request.form.get("password") or "").strip()

    if not username or not password:
        flash("请输入账号和密码", "warning")
        return redirect(url_for("auth.login"))

    try:
        user = DBManager.get_user_by_username(username)
    except Exception:
        flash("数据库连接失败，请检查配置", "danger")
        return redirect(url_for("auth.login"))
    if not user or not user.get("is_active"):
        flash("账号或密码错误", "danger")
        return redirect(url_for("auth.login"))

    stored = user.get("password_hash") or ""
    is_hash = stored.startswith("pbkdf2:") or stored.startswith("scrypt:") or stored.startswith("argon2:")
    ok = check_password_hash(stored, password) if is_hash else (stored == password)

    if ok:
        session["logged_in"] = True
        session["username"] = username
        DBManager.update_user_last_login(user.get("id"))
        return redirect(url_for("main.index"))

    flash("账号或密码错误", "danger")
    return redirect(url_for("auth.login"))


@auth_bp.route("/logout", methods=["GET"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
