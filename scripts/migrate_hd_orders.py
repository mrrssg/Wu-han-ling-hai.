# -*- coding: utf-8 -*-
"""HD订单表（经Teapplix同步，幂等建表）。"""
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import create_app
from app.models.db_manager import DBManager

DDL = """
CREATE TABLE IF NOT EXISTS order_system.hd_order_data (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    txn_id VARCHAR(64) NOT NULL COMMENT 'Teapplix TxnId(ch-hd-xxx)',
    line_number INT DEFAULT 1,
    store_key VARCHAR(32) COMMENT 'Teapplix StoreKey(区分HD多店)',
    invoice VARCHAR(64) COMMENT 'HD订单号',
    payment_date DATETIME,
    last_update DATETIME,
    shipped TINYINT DEFAULT 0,
    buyer_name VARCHAR(120), phone VARCHAR(40), email VARCHAR(160),
    street VARCHAR(255), street2 VARCHAR(255), city VARCHAR(80),
    state VARCHAR(40), zip VARCHAR(16),
    item_sku VARCHAR(64) COMMENT 'HD商品号(OrderItems.Name)',
    item_desc VARCHAR(400),
    quantity INT, amount DECIMAL(10,2), order_total DECIMAL(10,2),
    warehouse_sku VARCHAR(64) COMMENT '供应商SKU(OrderDetails.Custom)',
    custom2 VARCHAR(64),
    tracking_number VARCHAR(64),
    synced_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_line (txn_id, line_number),
    KEY idx_pay (payment_date), KEY idx_ship (shipped), KEY idx_sku (item_sku)
) CHARSET=utf8mb4 COMMENT='HD订单(Teapplix OrderNotification同步)'
"""


def main() -> int:
    app = create_app(os.environ.get("FLASK_CONFIG", "production"))
    with app.app_context():
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(DDL)
            conn.commit()
            print("hd_order_data schema OK")
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
