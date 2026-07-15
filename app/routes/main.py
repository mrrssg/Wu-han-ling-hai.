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


@main_bp.route('/')
def index():
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
    return render_template('index.html', kpi=kpi)


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
