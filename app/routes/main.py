from flask import Blueprint, render_template, url_for
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


def _q1(sql, params=None):
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params) if params else cur.execute(sql)
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


def _qall_p(sql, params):
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
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
    todos = []      # {icon, label, count, money, href, danger}
    unshipped = []  # {label, shipping, waiting}
    returns_y = {"total": 0, "rows": []}   # 昨日退货 per 店铺

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
                          "href": url_for("profit_control.actions"), "danger": True})

    def _todo_near_writeoff():
        r = _q1("""SELECT COUNT(*) AS n, COALESCE(SUM(exposure),0) AS v
                   FROM order_system.return_case
                   WHERE state='pending' AND supplier='Costway'
                     AND DATEDIFF(CURDATE(), return_date) >= 150""")
        if r and int(r["n"] or 0):
            todos.append({"icon": "⏰", "label": "追款临近180天核销线（等了≥150天）",
                          "count": int(r["n"]), "money": float(r["v"] or 0),
                          "href": url_for("profit_control.actions"), "danger": True})

    def _todo_sentinel():
        r = _q1("""SELECT COUNT(*) AS n FROM order_system.listing_sentinel_findings
                   WHERE verdict='severe' AND status='open'""")
        if r and int(r["n"] or 0):
            todos.append({"icon": "🔴", "label": "Listing严重不符（哨兵发现，未修复）",
                          "count": int(r["n"]), "money": None,
                          "href": url_for("profit_control.sentinel"), "danger": True})

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
                          "href": url_for("profit_control.issues"), "danger": danger})

    def _todo_repricing():
        # 待改价 = 每店最新 监控run(mon-)+分档定价run(plan-) 的 dry_run 去重合并，
        # 减去之后已 success 推价的（口径同候选页 _all_candidates）
        from app.services.repricing_stores import REPRICING_STORES
        for key, cfg in REPRICING_STORES.items():
            skus = set()
            for prefix in ("mon", "plan"):
                latest = _q1(
                    """SELECT run_id FROM order_system.offer_price_change_log
                       WHERE run_id LIKE %s AND status='dry_run'
                       ORDER BY triggered_at DESC LIMIT 1""", (f"{prefix}-{key}-%",))
                if not latest:
                    continue
                rows = _qall_p(
                    """SELECT log.shop_sku FROM order_system.offer_price_change_log log
                       WHERE log.run_id=%s AND log.status='dry_run'
                         AND NOT EXISTS (
                             SELECT 1 FROM order_system.offer_price_change_log later
                              WHERE later.shop_sku = log.shop_sku
                                AND later.store_key = %s
                                AND later.triggered_at > log.triggered_at
                                AND later.status = 'success')""",
                    (latest["run_id"], key))
                skus.update(r["shop_sku"] for r in rows)
            if skus:
                todos.append({"icon": "💲", "label": f"待改价（{cfg['label']}）",
                              "count": len(skus), "money": None,
                              "href": url_for("repricing.candidates_page", store=key),
                              "danger": False})

    def _unshipped():
        # 权威口径（用户 2026-07-15 定）：Mirakl同步表 order_state，
        # SHIPPING=已接单待发货，WAITING_ACCEPTANCE=还没接单（更急）。
        rows = _qall("""
            SELECT sc.platform AS label, d.order_state, COUNT(*) AS n FROM (
                SELECT shop_id, order_state FROM order_system.macy_order_data
                 WHERE order_state IN ('SHIPPING','WAITING_ACCEPTANCE')
                UNION ALL
                SELECT shop_id, order_state FROM order_system.lowes_order_data
                 WHERE order_state IN ('SHIPPING','WAITING_ACCEPTANCE')
                UNION ALL
                SELECT shop_id, order_state FROM order_system.bestbuy_order_data
                 WHERE order_state IN ('SHIPPING','WAITING_ACCEPTANCE')
            ) d JOIN order_system.shop_configs sc ON sc.id = d.shop_id
            GROUP BY sc.platform, d.order_state""")
        agg = {}
        for r in rows:
            a = agg.setdefault(r["label"], {"label": r["label"], "shipping": 0, "waiting": 0})
            if r["order_state"] == "SHIPPING":
                a["shipping"] = int(r["n"] or 0)
            else:
                a["waiting"] = int(r["n"] or 0)
        unshipped.extend(sorted(agg.values(), key=lambda x: -(x["shipping"] + x["waiting"])))

    def _returns_yesterday():
        rows = _qall("""
            SELECT CONCAT(platform, '-', shop_name) AS label, COUNT(*) AS n
            FROM order_system.mirakl_returns
            WHERE DATE(date_created) = DATE_SUB(CURDATE(), INTERVAL 1 DAY)
            GROUP BY platform, shop_name ORDER BY n DESC""")
        returns_y["rows"] = [{"label": r["label"], "n": int(r["n"] or 0)} for r in rows]
        returns_y["total"] = sum(r["n"] for r in returns_y["rows"])

    for fn in (_todo_unfiled, _todo_near_writeoff, _todo_sentinel, _todo_issues,
               _todo_repricing, _unshipped, _returns_yesterday):
        _safe(fn)

    return render_template('index.html', todos=todos, unshipped=unshipped,
                           returns_y=returns_y)


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
