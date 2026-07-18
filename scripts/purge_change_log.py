# -*- coding: utf-8 -*-
"""改价审计日志瘦身（每周日05:15）：dry_run/skipped 流水超90天删除，
success/failed/alert/blacklisted 永久保留（审计价值所在）。分批删防长锁。"""
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import create_app
from app.models.db_manager import DBManager

KEEP_DAYS = 90
BATCH = 5000


def main() -> int:
    app = create_app(os.environ.get("FLASK_CONFIG", "production"))
    with app.app_context():
        conn = DBManager.get_connection()
        total = 0
        try:
            while True:
                with conn.cursor() as cur:
                    cur.execute(
                        """DELETE FROM order_system.offer_price_change_log
                           WHERE status IN ('dry_run','skipped')
                             AND triggered_at < DATE_SUB(NOW(), INTERVAL %s DAY)
                           LIMIT %s""", (KEEP_DAYS, BATCH))
                    n = cur.rowcount
                conn.commit()
                total += n
                if n < BATCH:
                    break
            print(f"purged {total} rows (dry_run/skipped older than {KEEP_DAYS}d)")
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
