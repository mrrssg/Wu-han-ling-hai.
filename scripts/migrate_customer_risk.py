# -*- coding: utf-8 -*-
"""可疑客户分析建表（幂等）。"""
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import create_app
from app.models.db_manager import DBManager

DDL = """
CREATE TABLE IF NOT EXISTS order_system.customer_risk_profile (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    id_type VARCHAR(10) NOT NULL COMMENT 'email/phone/street',
    id_norm VARCHAR(255) NOT NULL COMMENT '归一后的身份键',
    display VARCHAR(255) COMMENT '展示用(原始邮箱/电话/街道)',
    names VARCHAR(500) COMMENT '该身份下出现过的姓名(去重)',
    stores VARCHAR(300),
    orders_n INT DEFAULT 0,
    returns_n INT DEFAULT 0,
    return_rate DECIMAL(6,4),
    not_charged_n INT DEFAULT 0 COMMENT '申请退货但钱没被扣的次数(蹭退特征)',
    reasons VARCHAR(500) COMMENT '退货原因分布 top',
    first_order DATE, last_order DATE,
    sample_orders VARCHAR(700),
    rep_name VARCHAR(120), rep_phone VARCHAR(40), rep_email VARCHAR(160),
    rep_street VARCHAR(255), rep_city VARCHAR(80), rep_state VARCHAR(40), rep_zip VARCHAR(16),
    risk_level VARCHAR(8) COMMENT 'high/mid/low',
    risk_reason VARCHAR(300),
    blacklisted TINYINT DEFAULT 0,
    built_at DATETIME,
    UNIQUE KEY uq_id (id_type, id_norm),
    KEY idx_risk (risk_level)
) CHARSET=utf8mb4 COMMENT='可疑客户风险档案(只存有退货的客户,每日重建)'
"""


def main() -> int:
    app = create_app(os.environ.get("FLASK_CONFIG", "production"))
    with app.app_context():
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(DDL)
            conn.commit()
            print("customer_risk schema OK")
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
