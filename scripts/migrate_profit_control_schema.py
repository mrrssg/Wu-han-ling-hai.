# -*- coding: utf-8 -*-
"""利润控制台建表迁移（幂等，可重复跑）。

Usage:
    PYTHONPATH=/var/www/autoweb/AutoWeb FLASK_CONFIG=production \
        ./venv/bin/python scripts/migrate_profit_control_schema.py
"""
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app import create_app
from app.models.db_manager import DBManager

DDL = [
    """
    CREATE TABLE IF NOT EXISTS order_system.profit_cell_daily (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        snapshot_date DATE NOT NULL,
        operator VARCHAR(32) NOT NULL,
        store VARCHAR(64) NOT NULL,
        orders_30d INT DEFAULT 0,
        sale_30d DECIMAL(12,2) DEFAULT 0,
        profit_30d DECIMAL(12,2) DEFAULT 0,
        margin_30d DECIMAL(8,4) DEFAULT NULL,
        orders_90d INT DEFAULT 0,
        sale_90d DECIMAL(12,2) DEFAULT 0,
        profit_gross_90d DECIMAL(12,2) DEFAULT 0 COMMENT '非退货单毛利合计',
        margin_90d_raw DECIMAL(8,4) DEFAULT NULL COMMENT '全部订单利润/销售(对齐旧Cell表)',
        confirmed_return_loss_90d DECIMAL(12,2) DEFAULT 0,
        pending_exposure_90d DECIMAL(12,2) DEFAULT 0 COMMENT '待追回货值敞口',
        expected_pending_loss_90d DECIMAL(12,2) DEFAULT 0 COMMENT '敞口×(1-回收率)',
        expected_future_loss_90d DECIMAL(12,2) DEFAULT 0 COMMENT '链梯法未成熟退货预扣',
        margin_90d_adj DECIMAL(8,4) DEFAULT NULL COMMENT '成熟度修正净利率(考核口径)',
        recovery_rate DECIMAL(8,4) DEFAULT NULL,
        returns_90d INT DEFAULT 0,
        return_rate_90d DECIMAL(8,4) DEFAULT NULL,
        ultimate_return_rate DECIMAL(8,4) DEFAULT NULL,
        baseline DECIMAL(8,4) DEFAULT 0.1000,
        gap_usd DECIMAL(12,2) DEFAULT 0,
        meets_baseline TINYINT DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uq_cell_date (snapshot_date, operator, store),
        KEY idx_date (snapshot_date)
    ) CHARACTER SET utf8mb4 COMMENT='利润控制台: cell级每日快照'
    """,
    """
    CREATE TABLE IF NOT EXISTS order_system.return_case (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        order_id VARCHAR(64) NOT NULL,
        order_line VARCHAR(16) NOT NULL DEFAULT '1',
        store VARCHAR(64), operator VARCHAR(32), supplier VARCHAR(32),
        shop_sku VARCHAR(64),
        order_date DATE, return_date DATE,
        cost DECIMAL(10,2) DEFAULT 0,
        return_fee DECIMAL(10,2) DEFAULT 0,
        supplier_refund DECIMAL(10,2) DEFAULT NULL COMMENT '只认飞书实际回填值',
        state ENUM('pending','recovered','written_off') DEFAULT 'pending',
        age_days INT DEFAULT 0,
        confirmed_loss DECIMAL(10,2) DEFAULT 0,
        exposure DECIMAL(10,2) DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uq_order_line (order_id, order_line),
        KEY idx_state (state), KEY idx_store (store), KEY idx_return_date (return_date)
    ) CHARACTER SET utf8mb4 COMMENT='利润控制台: 退货三态台账'
    """,
    """
    CREATE TABLE IF NOT EXISTS order_system.issue_log (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        detected_date DATE NOT NULL,
        issue_type VARCHAR(48) NOT NULL,
        entity VARCHAR(128) NOT NULL,
        severity VARCHAR(8) DEFAULT 'mid',
        impact_usd DECIMAL(12,2) DEFAULT 0,
        evidence TEXT,
        suggestion TEXT,
        status VARCHAR(16) DEFAULT 'open' COMMENT 'open/acked/resolved/stale',
        resolved_at DATETIME DEFAULT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uq_issue (detected_date, issue_type, entity),
        KEY idx_status (status), KEY idx_date (detected_date)
    ) CHARACTER SET utf8mb4 COMMENT='利润控制台: 问题清单'
    """,
    """
    CREATE TABLE IF NOT EXISTS order_system.action_log (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        action_type VARCHAR(32) NOT NULL COMMENT 'raise_price/delist/recover/reroute',
        target VARCHAR(128) NOT NULL,
        store VARCHAR(64),
        params_json TEXT,
        based_on_issue_id BIGINT DEFAULT NULL,
        status VARCHAR(16) DEFAULT 'proposed' COMMENT 'proposed/confirmed/executed/rolled_back/done',
        before_metrics TEXT, after_metrics TEXT,
        outcome VARCHAR(16) DEFAULT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        KEY idx_status (status), KEY idx_type (action_type)
    ) CHARACTER SET utf8mb4 COMMENT='利润控制台: 措施台账(Phase2使用)'
    """,
]


def main() -> int:
    config_name = os.environ.get("FLASK_CONFIG", "production")
    app = create_app(config_name)
    with app.app_context():
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cur:
                for ddl in DDL:
                    cur.execute(ddl)
            conn.commit()
            print("profit_control schema OK: profit_cell_daily / return_case / issue_log / action_log")
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
