"""
Idempotent schema migration for the Macy-kuyotq automated repricing system.

- Extends order_system.offerprice_listing with new columns and indexes.
- Creates four new tables:
    * order_system.offer_pricing_config        (Feishu config snapshot)
    * order_system.offer_price_change_log      (per-offer change history)
    * order_system.offer_alert_state           (blacklist + alert state per SKU)
    * order_system.repricing_full_sync_run     (Part 2 progress)

Safe to run multiple times - uses IF NOT EXISTS / information_schema checks.

Usage:
    PYTHONPATH=/var/www/autoweb/AutoWeb FLASK_CONFIG=production \
        ./venv/bin/python scripts/migrate_repricing_schema.py
"""
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app import create_app
from app.models.db_manager import DBManager


OFFERPRICE_NEW_COLUMNS = [
    ("cost_price", "DECIMAL(10,2) NULL"),
    ("origin_price", "DECIMAL(10,2) NULL"),
    ("discount_price", "DECIMAL(10,2) NULL"),
    ("discount_start_date", "DATE NULL"),
    ("discount_end_date", "DATE NULL"),
    ("state_code", "VARCHAR(20) NULL"),
    ("warehouse_sku", "VARCHAR(100) NULL"),
    ("last_cost_snapshot", "DECIMAL(10,2) NULL"),
    ("last_cost_snapshot_at", "DATETIME NULL"),
    ("active", "TINYINT(1) NULL"),
    ("raw_json", "LONGTEXT NULL"),
    ("source_export_id", "VARCHAR(64) NULL"),
]

OFFERPRICE_NEW_INDEXES = [
    ("idx_warehouse_sku", "(`warehouse_sku`)"),
    ("idx_status_active", "(`status`, `active`)"),
]


def _column_exists(cursor, schema, table, column):
    cursor.execute(
        """SELECT 1 FROM information_schema.columns
           WHERE table_schema=%s AND table_name=%s AND column_name=%s LIMIT 1""",
        (schema, table, column),
    )
    return cursor.fetchone() is not None


def _index_exists(cursor, schema, table, index_name):
    cursor.execute(
        """SELECT 1 FROM information_schema.statistics
           WHERE table_schema=%s AND table_name=%s AND index_name=%s LIMIT 1""",
        (schema, table, index_name),
    )
    return cursor.fetchone() is not None


def _table_exists(cursor, schema, table):
    cursor.execute(
        """SELECT 1 FROM information_schema.tables
           WHERE table_schema=%s AND table_name=%s LIMIT 1""",
        (schema, table),
    )
    return cursor.fetchone() is not None


def extend_offerprice_listing(cursor):
    print("[1/5] Extending order_system.offerprice_listing ...")
    for col, ddl in OFFERPRICE_NEW_COLUMNS:
        if _column_exists(cursor, "order_system", "offerprice_listing", col):
            print(f"      column `{col}` already exists, skip")
            continue
        print(f"      adding column `{col}` {ddl}")
        cursor.execute(
            f"ALTER TABLE order_system.offerprice_listing ADD COLUMN `{col}` {ddl}"
        )
    for idx_name, idx_cols in OFFERPRICE_NEW_INDEXES:
        if _index_exists(cursor, "order_system", "offerprice_listing", idx_name):
            print(f"      index `{idx_name}` already exists, skip")
            continue
        print(f"      adding index `{idx_name}` {idx_cols}")
        cursor.execute(
            f"ALTER TABLE order_system.offerprice_listing ADD INDEX `{idx_name}` {idx_cols}"
        )


def create_offer_pricing_config(cursor):
    print("[2/5] Creating order_system.offer_pricing_config ...")
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS order_system.offer_pricing_config (
            warehouse_sku         VARCHAR(100) NOT NULL PRIMARY KEY,
            store_key             VARCHAR(64)  NOT NULL,
            supplier              VARCHAR(32)  NULL,
            discount_factor       DECIMAL(5,4) NULL,
            commission_rate       DECIMAL(5,4) NULL,
            return_shipping_base  DECIMAL(10,2) NULL,
            length_in             DECIMAL(8,2) NULL,
            width_in              DECIMAL(8,2) NULL,
            height_in             DECIMAL(8,2) NULL,
            weight_lb             DECIMAL(8,2) NULL,
            feishu_record_id      VARCHAR(64)  NULL,
            last_synced_at        DATETIME     NOT NULL,
            INDEX idx_store (store_key),
            INDEX idx_synced (last_synced_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )


def create_offer_price_change_log(cursor):
    print("[3/5] Creating order_system.offer_price_change_log ...")
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS order_system.offer_price_change_log (
            id                    BIGINT AUTO_INCREMENT PRIMARY KEY,
            run_id                VARCHAR(64) NOT NULL,
            run_type              VARCHAR(32) NOT NULL,
            store_key             VARCHAR(64) NOT NULL,
            shop_sku              VARCHAR(100) NOT NULL,
            warehouse_sku         VARCHAR(100) NULL,
            triggered_at          DATETIME NOT NULL,

            status                VARCHAR(32) NOT NULL,
            decision_reason       VARCHAR(255) NULL,
            alert_type            VARCHAR(64) NULL,
            error_message         TEXT NULL,

            supplier              VARCHAR(32) NULL,
            supplier_price_db     DECIMAL(10,2) NULL,
            supplier_data_age_hours DECIMAL(8,2) NULL,
            costway_updated_at    DATETIME NULL,
            vevor_updated_at      DATETIME NULL,

            old_origin_price      DECIMAL(10,2) NULL,
            new_origin_price      DECIMAL(10,2) NULL,
            old_discount_price    DECIMAL(10,2) NULL,
            new_discount_price    DECIMAL(10,2) NULL,
            old_cost              DECIMAL(10,2) NULL,
            new_cost              DECIMAL(10,2) NULL,
            cost_change_pct       DECIMAL(8,4) NULL,

            discount_factor       DECIMAL(5,4) NULL,
            commission_rate       DECIMAL(5,4) NULL,
            return_shipping_base  DECIMAL(10,2) NULL,
            return_shipping_extra DECIMAL(10,2) NULL,
            return_cost_estimate  DECIMAL(10,2) NULL,
            total_cost            DECIMAL(10,2) NULL,

            profit_margin_before  DECIMAL(8,4) NULL,
            profit_margin_after   DECIMAL(8,4) NULL,
            formula_calc_price    DECIMAL(10,2) NULL,
            target_origin_price   DECIMAL(10,2) NULL,

            mirakl_called         TINYINT(1) NULL,
            mirakl_import_id      BIGINT NULL,
            mirakl_http_status    INT NULL,
            mirakl_response_body  TEXT NULL,
            mirakl_payload_hash   CHAR(64) NULL,

            verify_attempted_at   DATETIME NULL,
            verify_result         VARCHAR(64) NULL,

            ip_used               VARCHAR(64) NULL,
            api_call_seq          INT NULL,

            INDEX idx_sku_time (shop_sku, triggered_at DESC),
            INDEX idx_run (run_id),
            INDEX idx_status_time (status, triggered_at DESC),
            INDEX idx_pending_verify (status, triggered_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )


def create_offer_alert_state(cursor):
    print("[4/5] Creating order_system.offer_alert_state ...")
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS order_system.offer_alert_state (
            shop_sku            VARCHAR(100) NOT NULL PRIMARY KEY,
            store_key           VARCHAR(64) NOT NULL,
            failure_count       INT NOT NULL DEFAULT 0,
            blacklisted         TINYINT(1) NOT NULL DEFAULT 0,
            blacklisted_at      DATETIME NULL,
            blacklisted_reason  TEXT NULL,
            last_alert_type     VARCHAR(64) NULL,
            last_alert_message  TEXT NULL,
            last_alert_at       DATETIME NULL,
            resolved_at         DATETIME NULL,
            INDEX idx_blacklisted (blacklisted, store_key),
            INDEX idx_alert_time (last_alert_at DESC)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )


def create_repricing_full_sync_run(cursor):
    print("[5/5] Creating order_system.repricing_full_sync_run ...")
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS order_system.repricing_full_sync_run (
            run_id           VARCHAR(64) NOT NULL PRIMARY KEY,
            store_key        VARCHAR(64) NOT NULL,
            status           VARCHAR(32) NOT NULL,
            triggered_by     VARCHAR(64) NULL,
            total            INT NOT NULL DEFAULT 0,
            processed        INT NOT NULL DEFAULT 0,
            success_count    INT NOT NULL DEFAULT 0,
            failed_count     INT NOT NULL DEFAULT 0,
            skipped_count    INT NOT NULL DEFAULT 0,
            alerted_count    INT NOT NULL DEFAULT 0,
            started_at       DATETIME NOT NULL,
            finished_at      DATETIME NULL,
            last_progress_at DATETIME NOT NULL,
            error_message    TEXT NULL,
            INDEX idx_store_time (store_key, started_at DESC)
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
                extend_offerprice_listing(cursor)
                create_offer_pricing_config(cursor)
                create_offer_price_change_log(cursor)
                create_offer_alert_state(cursor)
                create_repricing_full_sync_run(cursor)
            conn.commit()
            print("\nAll migrations OK.")
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


if __name__ == "__main__":
    main()
