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
    return render_template('health.html', rows=rows, overall_ok=overall_ok)
