# -*- coding: utf-8 -*-
"""达标恒温器·周体检建表（幂等，2026-07-28）。

每周测各 Lowes cell(店铺×运营)的成熟净利率 vs 10%，给"提价/上调档/下架"建议。
只体检+建议，不自动改价（守定价铁律：推价走待改价页人工确认）。
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
CREATE TABLE IF NOT EXISTS order_system.thermostat_weekly (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    check_date DATE,
    store VARCHAR(24),
    operator VARCHAR(32),
    mature_months VARCHAR(24) COMMENT '用了哪两个成熟月',
    mature_sale DECIMAL(12,2),
    mature_net DECIMAL(12,2),
    mature_margin DECIMAL(6,4) COMMENT '成熟净利率',
    loss_rate DECIMAL(6,4) COMMENT '成熟退货损失率',
    gap DECIMAL(6,4) COMMENT '10%基线−成熟净利率(>0=没达标)',
    reprice_skus INT DEFAULT 0 COMMENT '待提价SKU数',
    reprice_uplift DECIMAL(12,2) DEFAULT 0 COMMENT '90天可补回毛利$',
    reprice_points DECIMAL(6,4) DEFAULT 0 COMMENT '提价能加多少个点',
    est_margin_after DECIMAL(6,4) COMMENT '执行提价后估算净利率',
    verdict VARCHAR(24) COMMENT '达标/提价即可达标/需上调档或下架',
    suggestion VARCHAR(800),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_cell (check_date, store, operator),
    KEY idx_date (check_date)
) CHARSET=utf8mb4 COMMENT='达标恒温器周体检(只体检+建议,不自动改价)'
"""


def main() -> int:
    app = create_app(os.environ.get("FLASK_CONFIG", "production"))
    with app.app_context():
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(DDL)
                for alter in (
                    "ADD COLUMN loss_rate DECIMAL(6,4) AFTER mature_margin",
                    "ADD COLUMN real_freight DECIMAL(12,2) COMMENT 'FedEx稽核真实退货运费(该运营已匹配票)' AFTER loss_rate",
                    "ADD COLUMN margin_after_freight DECIMAL(6,4) COMMENT '扣真实运费后净利率(保守下限)' AFTER real_freight",
                ):
                    try:
                        cur.execute(f"ALTER TABLE order_system.thermostat_weekly {alter}")
                    except Exception as exc:
                        if "Duplicate column" not in str(exc):
                            raise
            conn.commit()
            print("thermostat_weekly schema OK")
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
