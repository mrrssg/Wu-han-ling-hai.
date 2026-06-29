"""
Idempotent schema migration for the customer blacklist (客户黑名单筛查).

Creates order_system.customer_blacklist: one row per blacklisted customer /
address / contact. Used to screen 豪雅(Costway) / 司顺(Vevor) / 大建(Dajian)
unshipped-order exports so blacklisted orders are pulled out before the file
goes to the supplier for dropship.

Fields are all nullable except the surrogate id: a single entry may carry any
subset of identifying signals (a phone, an email, an address, a name+zip...).
Matching normalises both sides in Python (see blacklist_service), so we store
the raw values as entered.

Safe to run repeatedly (CREATE TABLE IF NOT EXISTS).

Usage:
    PYTHONPATH=/var/www/autoweb/AutoWeb FLASK_CONFIG=production \
        ./venv/bin/python scripts/migrate_blacklist_schema.py
"""
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app import create_app
from app.models.db_manager import DBManager


def create_customer_blacklist(cursor):
    print("[1/1] Creating order_system.customer_blacklist ...")
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS order_system.customer_blacklist (
            id          BIGINT AUTO_INCREMENT PRIMARY KEY,
            full_name   VARCHAR(255) NULL,
            phone       VARCHAR(64)  NULL,
            email       VARCHAR(255) NULL,
            street      VARCHAR(255) NULL,
            city        VARCHAR(128) NULL,
            state       VARCHAR(64)  NULL,
            zip         VARCHAR(32)  NULL,
            reason      VARCHAR(500) NULL,
            active      TINYINT(1)   NOT NULL DEFAULT 1,
            source      VARCHAR(16)  NOT NULL DEFAULT 'manual',
            created_by  VARCHAR(64)  NULL,
            created_at  DATETIME     NOT NULL,
            INDEX idx_active (active),
            INDEX idx_phone (phone),
            INDEX idx_email (email)
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
                create_customer_blacklist(cursor)
            conn.commit()
            print("\nCustomer-blacklist migration OK.")
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


if __name__ == "__main__":
    main()
