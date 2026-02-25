from flask import Blueprint, render_template

# 定义一个名为 'main' 的蓝图
main_bp = Blueprint('main', __name__)

@main_bp.route('/')
def index():
    # 渲染 templates/index.html
    return render_template('index.html')