"""
Idempotent schema migration for the repricing ORDER GUARD (成交价对账哨兵).

Creates order_system.order_guard_alert: one row per order line whose REAL sale
price (from {platform}_order_data) yields a margin below threshold when checked
against the latest supplier cost.

This is the safety net the once-daily, snapshot-based monitor cannot provide.
Incident that motivated it (ATCO-MDLW276312, 2026-06-23): the offer actually
transacted at $94.98 while the supplier cost was $217.49 (loss ~$137/unit), but
the daily OF52 snapshot only ever recorded the "correct" $633.96/$316.98 price,
so the monitor computed a healthy 14.46% margin and skipped it every single day.
The guard keys off ACTUAL orders, so it cannot be fooled by a stale snapshot.

Safe to run repeatedly (CREATE TABLE IF NOT EXISTS).

Usage:
    PYTHONPATH=/var/www/autoweb/AutoWeb FLASK_CONFIG=production \
        ./venv/bin/python scripts/migrate_order_guard_schema.py
"""
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app import create_app
from app.models.db_manager import DBManager


def create_order_guard_alert(cursor):
    print("[1/1] Creating order_system.order_guard_alert ...")
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS order_system.order_guard_alert (
            id               BIGINT AUTO_INCREMENT PRIMARY KEY,
            store_key        VARCHAR(64)  NOT NULL,
            platform         VARCHAR(32)  NOT NULL,
            shop_name        VARCHAR(64)  NOT NULL,
            order_id         VARCHAR(64)  NOT NULL,
            order_line_id    VARCHAR(80)  NOT NULL,
            order_created    DATETIME     NULL,
            order_state      VARCHAR(32)  NULL,
            shop_sku         VARCHAR(100) NOT NULL,
            warehouse_sku    VARCHAR(100) NULL,
            supplier         VARCHAR(32)  NULL,
            supplier_price   DECIMAL(10,2) NULL,
            unit_cost        DECIMAL(10,2) NULL,
            sale_price_unit  DECIMAL(10,2) NULL,
            expected_price   DECIMAL(10,2) NULL,
            quantity         INT          NULL,
            commission_fee   DECIMAL(10,2) NULL,
            return_cost_est  DECIMAL(10,2) NULL,
            line_revenue     DECIMAL(10,2) NULL,
            line_profit      DECIMAL(10,2) NULL,
            margin           DECIMAL(8,4) NULL,
            severity         VARCHAR(16)  NOT NULL,
            detected_at      DATETIME     NOT NULL,
            notified_at      DATETIME     NULL,
            resolved_at      DATETIME     NULL,
            UNIQUE KEY uq_line (store_key, order_line_id),
            INDEX idx_store_sev_time (store_key, severity, detected_at DESC),
            INDEX idx_notify (notified_at),
            INDEX idx_sku (shop_sku)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )


def main():
    config_name = os.environ.get("FLASK_CONFIG", "production")
    app = create_app(config_name)
    with app.app_context():
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cursor:
                create_order_guard_alert(cursor)
            conn.commit()
            print("\nOrder-guard migration OK.")
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


if __name__ == "__main__":
    main()
