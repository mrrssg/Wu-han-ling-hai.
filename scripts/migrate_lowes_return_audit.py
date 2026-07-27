# -*- coding: utf-8 -*-
"""Lowes-Autool 退货运费稽核板块建表（幂等，2026-07-27）。

上传 FedEx 发票 → 每票退货匹配 Lowes 订单(跟踪号→PO→推断候选) →
看清 退货运费损失 + 豪雅已登记退货货值(飞书退货登记表)。
"""
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import create_app
from app.models.db_manager import DBManager

DDL = """
CREATE TABLE IF NOT EXISTS order_system.fedex_return_audit (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    tracking VARCHAR(40) NOT NULL COMMENT 'FedEx跟踪号',
    ship_date DATE,
    net_charge DECIMAL(10,2) COMMENT '退货运费(实际扣费)',
    po VARCHAR(32) COMMENT '发票PO号(Original Ref#3)',
    cust_ref VARCHAR(120),
    shipper_name VARCHAR(120) COMMENT '寄件人(客户或门店)',
    shipper_city VARCHAR(80),
    shipper_state VARCHAR(16),
    shipper_zip VARCHAR(16),
    actual_weight DECIMAL(8,2),
    dim VARCHAR(40) COMMENT '箱规LxWxH',
    -- 匹配结果
    match_type VARCHAR(16) DEFAULT 'none' COMMENT 'tracking/po/inferred/manual/none',
    order_id VARCHAR(40),
    shop_sku VARCHAR(64),
    cost DECIMAL(10,2) COMMENT '货值(成本=供应商价×0.75)',
    sale DECIMAL(10,2),
    operator VARCHAR(32),
    claim_filed TINYINT DEFAULT NULL COMMENT '豪雅已登记(飞书退货登记表)1是0否NULL未知',
    candidates_json TEXT COMMENT '推断候选[{order_id,sku,cost,reason,score}]',
    confirmed TINYINT DEFAULT 0 COMMENT '用户已确认此匹配',
    invoice_file VARCHAR(160),
    uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    matched_at DATETIME,
    UNIQUE KEY uq_tracking (tracking),
    KEY idx_match (match_type), KEY idx_order (order_id), KEY idx_claim (claim_filed)
) CHARSET=utf8mb4 COMMENT='Lowes-Autool退货运费稽核(FedEx发票×订单×已登记)'
"""


def main() -> int:
    app = create_app(os.environ.get("FLASK_CONFIG", "production"))
    with app.app_context():
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(DDL)
            conn.commit()
            print("fedex_return_audit schema OK")
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
