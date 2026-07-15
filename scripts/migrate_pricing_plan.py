# -*- coding: utf-8 -*-
"""定价方案（分档定价）建表，幂等。

pricing_tier：每个在卖offer一行 = 档位 + 人话原因 + 数字证据。
评档脚本每周重评（UPSERT），status 由人工确认流转，重评不覆盖人工状态。
"""
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app import create_app
from app.models.db_manager import DBManager

DDL = """
CREATE TABLE IF NOT EXISTS order_system.pricing_tier (
    store_key      VARCHAR(64)  NOT NULL,
    shop_sku       VARCHAR(100) NOT NULL,
    tier           VARCHAR(24)  NOT NULL COMMENT 'standard/risk/repair/delist/cold_watch/cold_probe',
    target_margin  DECIMAL(5,4) DEFAULT NULL COMMENT '本档目标毛利率(冷启动=试探毛利)',
    reason_text    VARCHAR(500) COMMENT '人话原因：为什么进这一档',
    evidence_json  TEXT COMMENT '数字证据(90天单数/退货/单件利润/单退损失/上架天数等)',
    data_suspect   TINYINT(1) DEFAULT 0 COMMENT '评档输入数据存疑(如账本口径未定案的订单)',
    orders_90d     INT DEFAULT 0,
    returns_90d    INT DEFAULT 0,
    margin_90d     DECIMAL(8,4) DEFAULT NULL COMMENT '90天净利率(含退货损失)',
    gross_margin   DECIMAL(8,4) DEFAULT NULL COMMENT '90天毛利率(非退货单)',
    listed_days    INT DEFAULT NULL,
    cur_price      DECIMAL(10,2) DEFAULT NULL COMMENT '当前折扣价(无折扣=原价)',
    cost_price     DECIMAL(10,2) DEFAULT NULL,
    status         VARCHAR(16) DEFAULT 'proposed' COMMENT 'proposed/confirmed/paused',
    assigned_at    DATETIME,
    updated_at     DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (store_key, shop_sku),
    KEY idx_tier (store_key, tier)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='分档定价：档位+原因+证据，评档脚本UPSERT，人工确认后进改价管道'
"""


ALTERS = [
    "ALTER TABLE order_system.pricing_tier ADD COLUMN loss_rate DECIMAL(8,4) DEFAULT NULL "
    "COMMENT '预期退货损失率(占销售额,已按可要回比例p折减)'",
    "ALTER TABLE order_system.pricing_tier ADD COLUMN rate_source VARCHAR(16) DEFAULT NULL "
    "COMMENT '损失率数据来源: SKU自己/类目/全店'",
]


def main() -> int:
    app = create_app(os.environ.get("FLASK_CONFIG", "production"))
    with app.app_context():
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(DDL)
                for alter in ALTERS:
                    try:
                        cur.execute(alter)
                    except Exception as exc:
                        if "Duplicate column" not in str(exc):
                            raise
            conn.commit()
            print("pricing_tier schema OK")
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
