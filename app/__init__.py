from flask import Flask, session, redirect, url_for, request
# 引入配置文件（假设你根目录下有 config.py）
from config import config


def create_app(config_name='default'):
    app = Flask(__name__)

    # 加载配置 (密钥、数据库密码等)
    app.config.from_object(config.get(config_name))

    # --- 注册蓝图 ---
    from app.routes.auth import auth_bp
    app.register_blueprint(auth_bp)

    from app.routes.main import main_bp
    from app.routes.orders import orders_bp  # 假设你之前写好了 orders.py

    app.register_blueprint(main_bp)
    app.register_blueprint(orders_bp, url_prefix='/orders')  # 访问订单功能前缀是 /orders

    from app.routes.stock import stock_bp
    app.register_blueprint(stock_bp, url_prefix='/stock')

    from app.routes.ai_fill import ai_fill_bp
    app.register_blueprint(ai_fill_bp, url_prefix='/ai-fill')

    from app.routes.repricing import repricing_bp
    app.register_blueprint(repricing_bp, url_prefix='/repricing')

    from app.routes.hd_shipping import hd_shipping_bp
    app.register_blueprint(hd_shipping_bp, url_prefix='/hd-shipping')

    from app.routes.profit_control import profit_control_bp
    app.register_blueprint(profit_control_bp, url_prefix='/profit-control')

    from app.routes.preview import preview_bp
    app.register_blueprint(preview_bp, url_prefix='/preview')  # 新版界面样本，定稿后下线

    from app.routes.assistant import assistant_bp
    app.register_blueprint(assistant_bp, url_prefix='/assistant')  # 右下角AI助理

    from app.routes.safety import safety_bp
    app.register_blueprint(safety_bp, url_prefix='/safety')  # 产品安全防控

    from app.routes.customer_risk import customer_risk_bp
    app.register_blueprint(customer_risk_bp, url_prefix='/customer-risk')  # 可疑客户分析

    from app.routes.macy_selection import macy_selection_bp
    app.register_blueprint(macy_selection_bp, url_prefix='/macy-selection')  # Macy选品

    from app.routes.lowes_selection import lowes_selection_bp
    app.register_blueprint(lowes_selection_bp, url_prefix='/lowes-selection')  # Lowes选品

    @app.before_request
    def require_login():
        endpoint = request.endpoint or ""
        if endpoint.startswith("static") or endpoint.startswith("auth."):
            return
        if not session.get("logged_in"):
            return redirect(url_for("auth.login"))

    return app
