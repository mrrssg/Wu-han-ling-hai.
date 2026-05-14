from flask import Blueprint, render_template
from app.models.db_manager import DBManager

# 定义一个名为 'main' 的蓝图
main_bp = Blueprint('main', __name__)


# 各店铺 cursor 不更新的告警阈值（小时）。
# lowes_autool 订单量极少,放宽到 7 天;其它店 6 小时。
STALE_THRESHOLD_HOURS = {
    "lowes_autool": 168,
}
DEFAULT_STALE_HOURS = 6


@main_bp.route('/')
def index():
    return render_template('index.html')


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
