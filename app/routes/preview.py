"""新版界面（领星式左侧菜单布局）预览路由。

样本阶段：/preview/ = 新版仪表盘，/preview/orders = 新版订单查询。
业务逻辑全部复用现有实现，只换壳（base_v2.html）；
定稿后各页面把 extends 从 base.html 切到 base_v2.html 即完成迁移，本蓝图随之下线。
"""
from flask import Blueprint, render_template, request

from app.models.db_manager import DBManager

preview_bp = Blueprint("preview", __name__)


def _q1(sql, params=None):
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params) if params else cur.execute(sql)
            return cur.fetchone()
    finally:
        conn.close()


@preview_bp.route("/")
def home():
    kpi = {"sale_1d": 0.0, "net_1d": 0.0, "margin30": None, "returns_7d": 0,
           "open_issues": 0, "unfiled_recover": 0, "snap_date": None, "trend_date": None}
    try:
        r = _q1("""SELECT stat_date, sale_1d, net_1d, rolling30_margin
                   FROM order_system.profit_trend_daily
                   WHERE scope='公司' AND stat_date < CURDATE()
                   ORDER BY stat_date DESC LIMIT 1""")
        if r:
            kpi["sale_1d"] = float(r["sale_1d"] or 0)
            kpi["net_1d"] = float(r["net_1d"] or 0)
            kpi["margin30"] = float(r["rolling30_margin"]) if r["rolling30_margin"] is not None else None
            kpi["trend_date"] = str(r["stat_date"])
        r = _q1("""SELECT COUNT(*) AS n FROM order_system.return_case
                   WHERE return_date >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
                     AND state <> 'not_charged'""")
        kpi["returns_7d"] = int(r["n"] or 0) if r else 0
        r = _q1("SELECT COUNT(*) AS n FROM order_system.issue_log WHERE status='open'")
        kpi["open_issues"] = int(r["n"] or 0) if r else 0
        r = _q1("""SELECT COUNT(*) AS n FROM order_system.return_case
                   WHERE state='pending' AND supplier='Costway' AND cost >= 20
                     AND claim_filed=0 AND DATEDIFF(CURDATE(), order_date) <= 90""")
        kpi["unfiled_recover"] = int(r["n"] or 0) if r else 0
        r = _q1("SELECT MAX(snapshot_date) AS d FROM order_system.profit_cell_daily")
        kpi["snap_date"] = str(r["d"]) if r and r["d"] else None
    except Exception:
        pass   # 首页永不因看板数据挂掉
    return render_template("preview/home_v2.html", kpi=kpi)


@preview_bp.route("/orders", methods=["GET", "POST"])
def orders():
    results = []
    keyword = ""
    searched = False
    if request.method == "POST":
        keyword = request.form.get("keyword", "").strip()
        if keyword:
            searched = True
            results = DBManager.search_orders(keyword)
    return render_template("preview/search_v2.html",
                           results=results, keyword=keyword, searched=searched)
