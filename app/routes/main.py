from flask import Blueprint, render_template
from app.models.db_manager import DBManager

# 定义一个名为 'main' 的蓝图
main_bp = Blueprint('main', __name__)


# 各店铺 cursor 不更新的告警阈值（小时）。
# lowes 店订单量极少,放宽到 7 天;其它店 6 小时。
STALE_THRESHOLD_HOURS = {
    "lowes_autool": 168,
    "lowes_yasonic": 168,
}
DEFAULT_STALE_HOURS = 6


def _q1(sql):
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchone()
    finally:
        conn.close()


def _qall(sql):
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall() or []
    finally:
        conn.close()


ISSUE_TYPE_NAMES = {
    "cell_below_baseline": "利润率破线",
    "negative_ev_sku": "负期望SKU",
    "recovery_overdue": "追款超期",
    "data_stale": "数据回填滞后",
    "return_spike": "退货异动",
    "delisted_but_selling": "已下架仍出单",
    "listing_mismatch": "Listing不符",
}


@main_bp.route('/')
def index():
    """待办工作台：销售看飞书看板、利润看利润控制台，首页只放
    ①今天要处理的报警 ②未发货 ③利润一眼 ④常用入口。每项查询独立容错。"""
    todos = []      # {icon, label, count, money, url, danger}
    profit = {"net_1d": None, "margin30": None, "mtd_net": None, "mtd_margin": None}
    unshipped = []  # {label, n}

    def _safe(fn):
        try:
            fn()
        except Exception:
            pass

    def _todo_unfiled():
        r = _q1("""SELECT COUNT(*) AS n, COALESCE(SUM(exposure),0) AS v
                   FROM order_system.return_case
                   WHERE state='pending' AND supplier='Costway' AND cost >= 20
                     AND claim_filed=0 AND DATEDIFF(CURDATE(), order_date) <= 90""")
        if r and int(r["n"] or 0):
            todos.append({"icon": "❗", "label": "追款漏网（退货了还没登记）",
                          "count": int(r["n"]), "money": float(r["v"] or 0),
                          "url": "profit_control.actions", "danger": True})

    def _todo_near_writeoff():
        r = _q1("""SELECT COUNT(*) AS n, COALESCE(SUM(exposure),0) AS v
                   FROM order_system.return_case
                   WHERE state='pending' AND supplier='Costway'
                     AND DATEDIFF(CURDATE(), return_date) >= 150""")
        if r and int(r["n"] or 0):
            todos.append({"icon": "⏰", "label": "追款临近180天核销线（等了≥150天）",
                          "count": int(r["n"]), "money": float(r["v"] or 0),
                          "url": "profit_control.actions", "danger": True})

    def _todo_sentinel():
        r = _q1("""SELECT COUNT(*) AS n FROM order_system.listing_sentinel_findings
                   WHERE verdict='severe' AND status='open'""")
        if r and int(r["n"] or 0):
            todos.append({"icon": "🔴", "label": "Listing严重不符（哨兵发现，未修复）",
                          "count": int(r["n"]), "money": None,
                          "url": "profit_control.sentinel", "danger": True})

    def _todo_issues():
        rows = _qall("""SELECT issue_type, COUNT(*) AS n, COALESCE(SUM(impact_usd),0) AS v
                        FROM order_system.issue_log WHERE status='open'
                        GROUP BY issue_type ORDER BY v DESC""")
        for r in rows:
            t = r["issue_type"]
            danger = t == "delisted_but_selling"
            todos.append({"icon": "🚨" if danger else "📋",
                          "label": ISSUE_TYPE_NAMES.get(t, t),
                          "count": int(r["n"] or 0), "money": float(r["v"] or 0),
                          "url": "profit_control.issues", "danger": danger})

    def _profit():
        r = _q1("""SELECT net_1d, rolling30_margin FROM order_system.profit_trend_daily
                   WHERE scope='公司' AND stat_date < CURDATE()
                   ORDER BY stat_date DESC LIMIT 1""")
        if r:
            profit["net_1d"] = float(r["net_1d"] or 0)
            profit["margin30"] = float(r["rolling30_margin"]) if r["rolling30_margin"] is not None else None
        r = _q1("""SELECT SUM(sale) AS s, SUM(net) AS n FROM order_system.profit_month_cohort
                   WHERE order_month = DATE_FORMAT(CURDATE(), '%Y-%m')""")
        if r and r["s"] and float(r["s"]) > 0:
            profit["mtd_net"] = float(r["n"] or 0)
            profit["mtd_margin"] = float(r["n"] or 0) / float(r["s"])

    # 未发货卡已下线：平台订单表的 Status 有大量历史遗留空值（走其它发货路径未回写），
    # 数出来的不是真实待发货。等用户给出权威口径后再上。
    for fn in (_todo_unfiled, _todo_near_writeoff, _todo_sentinel, _todo_issues,
               _profit):
        _safe(fn)

    return render_template('index.html', todos=todos, profit=profit, unshipped=unshipped)


@main_bp.route('/feishu-dashboard')
def feishu_dashboard():
    """嵌入飞书多维表格仪表盘（实时数据）。链接存 instance/feishu_dashboard_url.txt，
    一行一个：标签|分享链接，改文件即生效不用重启。"""
    import os
    from flask import current_app
    boards = []
    path = os.path.join(current_app.instance_path, "feishu_dashboard_url.txt")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "|" in line:
                    label, url = line.split("|", 1)
                else:
                    label, url = "飞书仪表盘", line
                boards.append({"label": label.strip(), "url": url.strip()})
    except FileNotFoundError:
        pass
    return render_template('feishu_dashboard.html', boards=boards)


@main_bp.route('/health')
def health():
    conn = DBManager.get_connection()
    rows = []
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT store_key,
                       last_synced_at,
                       updated_at,
                       TIMESTAMPDIFF(MINUTE, updated_at, NOW()) AS minutes_stale
                FROM order_system.txn_sync_cursor
                ORDER BY store_key
                """
            )
            for r in cursor.fetchall():
                store = r['store_key']
                minutes_stale = r['minutes_stale'] or 0
                hours_stale = round(minutes_stale / 60, 1)
                threshold = STALE_THRESHOLD_HOURS.get(store, DEFAULT_STALE_HOURS)
                is_stale = minutes_stale > threshold * 60
                rows.append({
                    'store_key': store,
                    'last_synced_at': r['last_synced_at'],
                    'updated_at': r['updated_at'],
                    'hours_stale': hours_stale,
                    'threshold_hours': threshold,
                    'is_stale': is_stale,
                })
    finally:
        conn.close()

    overall_ok = all(not r['is_stale'] for r in rows)

    # Offer 全量/增量同步状态（offer_sync_cursor）
    offer_sync_rows = []
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT store_key, last_request_date, last_run_at,
                       last_tracking_id, last_offer_count, last_new_count,
                       last_status,
                       TIMESTAMPDIFF(MINUTE, last_run_at, NOW()) AS minutes_since
                FROM order_system.offer_sync_cursor
                ORDER BY store_key
                """
            )
            for r in cursor.fetchall():
                minutes_since = r['minutes_since'] or 0
                status = (r.get('last_status') or '').lower()
                # 成功标志：completed / completed_no_data 都算成功
                is_ok = status.startswith('completed')
                # 超过 30 小时没跑也标红（cron 是每天一次）
                is_stale = minutes_since > 30 * 60
                offer_sync_rows.append({
                    'store_key': r['store_key'],
                    'last_run_at': r['last_run_at'],
                    'hours_since': round(minutes_since / 60, 1),
                    'last_offer_count': r.get('last_offer_count') or 0,
                    'last_new_count': r.get('last_new_count') or 0,
                    'last_status': r.get('last_status') or '',
                    'is_ok': is_ok,
                    'is_stale': is_stale,
                })
    except Exception:
        # offer_sync_cursor 表可能还没建（首次同步前）
        offer_sync_rows = []
    finally:
        conn.close()

    return render_template(
        'health.html',
        rows=rows,
        overall_ok=overall_ok,
        offer_sync_rows=offer_sync_rows,
    )
