"""
Idempotent schema migration: create autooperate.hd_label_records.

Stores audit + result of Teapplix label purchases for HD Vevor orders.
Safe to run multiple times.

Usage (server, as admin user):
    PYTHONPATH=/var/www/autoweb/AutoWeb FLASK_CONFIG=production \\
        ./venv/bin/python scripts/migrate_hd_label_records.py
"""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app import create_app
from app.models.db_manager import DBManager


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS hd_label_records (
  txn_id              VARCHAR(64)    NOT NULL,
  store_key           VARCHAR(16)    NULL,
  shop_sku            VARCHAR(64)    NULL,
  warehouse_sku       VARCHAR(64)    NULL,
  profile_id          INT            NULL,
  warehouse_id        INT            NULL          COMMENT '10=NJ3, 432=CA6',
  shipping_channel_id INT            NULL          COMMENT 'Vevor 渠道ID 3808/3801',
  method              VARCHAR(32)    NULL          COMMENT 'UPS_GROUND etc',
  weight_lb           DECIMAL(8,2)   NULL,
  length_in           DECIMAL(8,2)   NULL,
  width_in            DECIMAL(8,2)   NULL,
  depth_in            DECIMAL(8,2)   NULL,
  quantity            INT            NULL,
  order_total         DECIMAL(10,2)  NULL,
  recipient_name      VARCHAR(128)   NULL,
  recipient_state     VARCHAR(8)     NULL,
  recipient_zip       VARCHAR(16)    NULL,
  tracking_number     VARCHAR(64)    NULL,
  label_url           VARCHAR(500)   NULL,
  postage             DECIMAL(8,2)   NULL,
  provider            VARCHAR(16)    NULL,
  status              VARCHAR(16)    NOT NULL DEFAULT 'pending'
                                     COMMENT 'pending/success/failed/cancelled',
  error_code          INT            NULL,
  error_msg           TEXT           NULL,
  request_json        JSON           NULL,
  response_json       JSON           NULL,
  excel_row_tsv       TEXT           NULL          COMMENT '35 cols tab-separated, ready to paste',
  cancelled_at        DATETIME       NULL,
  cancel_reason       TEXT           NULL,
  refund_amount       DECIMAL(8,2)   NULL,
  created_at          DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at          DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP
                                     ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (txn_id),
  INDEX idx_status_created (status, created_at),
  INDEX idx_tracking (tracking_number)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='HD Vevor 出面单审计 + 结果缓存';
"""


def _table_exists(cursor, schema, table):
    cursor.execute(
        """SELECT 1 FROM information_schema.tables
           WHERE table_schema=%s AND table_name=%s LIMIT 1""",
        (schema, table),
    )
    return cursor.fetchone() is not None


def main():
    app = create_app()
    with app.app_context():
        schema = app.config["DB_NAME"]
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cur:
                existed = _table_exists(cur, schema, "hd_label_records")
                cur.execute(CREATE_TABLE_SQL)
                if existed:
                    print("  hd_label_records already exists (CREATE TABLE IF NOT EXISTS = no-op)")
                else:
                    print("  hd_label_records created")
            conn.commit()
            print("Done.")
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


if __name__ == "__main__":
    main()
