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
    CREATE TABLE IF NOT EXISTS order_system.profit_trend_daily (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        scope VARCHAR(32) NOT NULL COMMENT '公司 或 运营名',
        stat_date DATE NOT NULL,
        sale_1d DECIMAL(12,2) DEFAULT 0,
        net_1d DECIMAL(12,2) DEFAULT 0 COMMENT '当日净贡献=非退货毛利-退货期望损失(下单口径)',
        rolling30_sale DECIMAL(12,2) DEFAULT 0,
        rolling30_net DECIMAL(12,2) DEFAULT 0,
        rolling30_margin DECIMAL(8,4) DEFAULT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uq_scope_date (scope, stat_date),
        KEY idx_date (stat_date)
    ) CHARACTER SET utf8mb4 COMMENT='利润控制台: 每日趋势序列'
    """,
    """
    CREATE TABLE IF NOT EXISTS order_system.profit_sku_90d (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        shop_sku VARCHAR(64) NOT NULL,
        store VARCHAR(64) NOT NULL,
        operator VARCHAR(32), supplier VARCHAR(32),
        orders INT DEFAULT 0,
        sale DECIMAL(12,2) DEFAULT 0,
        profit_gross DECIMAL(12,2) DEFAULT 0,
        returns_cnt INT DEFAULT 0,
        loss_expected DECIMAL(12,2) DEFAULT 0,
        net DECIMAL(12,2) DEFAULT 0,
        margin DECIMAL(8,4) DEFAULT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uq_sku_store (shop_sku, store),
        KEY idx_net (net), KEY idx_margin (margin)
    ) CHARACTER SET utf8mb4 COMMENT='利润控制台: SKU 90天指标(每日全量重建)'
    """,
    """
    CREATE TABLE IF NOT EXISTS order_system.profit_month_cohort (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        order_month CHAR(7) NOT NULL COMMENT '订单所属月 YYYY-MM',
        operator VARCHAR(32) NOT NULL,
        orders INT DEFAULT 0,
        sale DECIMAL(12,2) DEFAULT 0,
        profit_gross DECIMAL(12,2) DEFAULT 0 COMMENT '非退货单毛利',
        returns_cnt INT DEFAULT 0,
        loss_expected DECIMAL(12,2) DEFAULT 0 COMMENT '该月订单的退货期望损失(至今累计)',
        net DECIMAL(12,2) DEFAULT 0,
        margin_gross DECIMAL(8,4) DEFAULT NULL,
        margin_net DECIMAL(8,4) DEFAULT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uq_month_op (order_month, operator)
    ) CHARACTER SET utf8mb4 COMMENT='利润控制台: 订单月cohort(每日全量重建)'
    """,
    """
    CREATE TABLE IF NOT EXISTS order_system.profit_neg_orders (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        order_month CHAR(7) NOT NULL,
        operator VARCHAR(32), store VARCHAR(64),
        order_id VARCHAR(64), shop_sku VARCHAR(64),
        order_date DATE,
        sale DECIMAL(10,2), cost DECIMAL(10,2), profit DECIMAL(10,2),
        is_actual TINYINT DEFAULT 0 COMMENT '1=实际利润 0=预估',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        KEY idx_cell (order_month, operator, store)
    ) CHARACTER SET utf8mb4 COMMENT='利润控制台: 亏本卖的正常单明细(每日重建,诊断查定价用)'
    """,
    """
    CREATE TABLE IF NOT EXISTS order_system.listing_sentinel_findings (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        audit_date DATE NOT NULL,
        store VARCHAR(64), operator VARCHAR(32), supplier VARCHAR(32),
        shop_sku VARCHAR(64) NOT NULL, supplier_sku VARCHAR(64),
        returns_recent INT DEFAULT 0,
        reason_dist VARCHAR(255),
        price_ours DECIMAL(10,2), price_supplier DECIMAL(10,2), price_ratio DECIMAL(6,2),
        verdict VARCHAR(12) COMMENT 'clean/minor/severe',
        summary TEXT,
        issues_json MEDIUMTEXT,
        image_note VARCHAR(500),
        our_title VARCHAR(500), supplier_title VARCHAR(500),
        item_link VARCHAR(500),
        status VARCHAR(16) DEFAULT 'open' COMMENT 'open/fixed/false_positive',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uq_sku_store (shop_sku, store),
        KEY idx_date (audit_date), KEY idx_verdict (verdict)
    ) CHARACTER SET utf8mb4 COMMENT='Listing哨兵: 退货触发的listing三方对比结果'
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


ALTERS = [
    "ALTER TABLE order_system.return_case ADD COLUMN sale DECIMAL(10,2) DEFAULT NULL COMMENT '售价(整单总价)'",
    "ALTER TABLE order_system.return_case ADD COLUMN income_actual DECIMAL(10,2) DEFAULT NULL "
    "COMMENT '实际到账快照(买家退款入账后≈0; >1=退款未入账; NULL=账单未导入)'",
    "ALTER TABLE order_system.return_case ADD COLUMN profit_actual DECIMAL(10,2) DEFAULT NULL "
    "COMMENT '飞书实际利润快照(仍>0=退货后果未体现在账上)'",
    "ALTER TABLE order_system.return_case MODIFY COLUMN state "
    "ENUM('pending','recovered','written_off','not_charged') DEFAULT 'pending' "
    "COMMENT 'not_charged=有退货标记但账单未扣款,按正常单计'",
    "ALTER TABLE order_system.profit_month_cohort ADD COLUMN loss_actual DECIMAL(12,2) DEFAULT 0 "
    "COMMENT '实际口径损失(供应商退款只认已到账)'",
    "ALTER TABLE order_system.profit_month_cohort ADD COLUMN net_actual DECIMAL(12,2) DEFAULT 0 "
    "COMMENT '目前实际落袋=毛利-实际口径损失'",
    "ALTER TABLE order_system.profit_month_cohort ADD COLUMN margin_net_actual DECIMAL(8,4) DEFAULT NULL",
    "ALTER TABLE order_system.profit_month_cohort ADD COLUMN gross_est DECIMAL(12,2) DEFAULT 0 "
    "COMMENT '毛利中暂按预估占位的部分(账单未到/残缺)'",
    "ALTER TABLE order_system.return_case ADD COLUMN recover_note VARCHAR(255) DEFAULT NULL "
    "COMMENT '追款备注(人工填,每日重建不覆盖)'",
    "ALTER TABLE order_system.return_case ADD COLUMN note_time DATETIME DEFAULT NULL",
    "ALTER TABLE order_system.profit_month_cohort ADD COLUMN store VARCHAR(64) NOT NULL DEFAULT '' AFTER operator",
    "ALTER TABLE order_system.profit_month_cohort ADD COLUMN neg_profit DECIMAL(12,2) DEFAULT 0 "
    "COMMENT '亏本卖的正常单利润合计(负数,诊断用)'",
    "ALTER TABLE order_system.profit_month_cohort ADD COLUMN neg_n INT DEFAULT 0",
    "ALTER TABLE order_system.profit_month_cohort DROP INDEX uq_month_op",
    "ALTER TABLE order_system.profit_month_cohort ADD UNIQUE KEY uq_mos (order_month, operator, store)",
    "ALTER TABLE order_system.listing_sentinel_findings ADD COLUMN fix_json MEDIUMTEXT "
    "COMMENT 'AI修复方案(字段级现文案→改后文案+操作指引)'",
    "ALTER TABLE order_system.listing_sentinel_findings ADD COLUMN fix_time DATETIME DEFAULT NULL",
    "ALTER TABLE order_system.return_case ADD COLUMN claim_filed TINYINT(1) NOT NULL DEFAULT 0 "
    "COMMENT '已在飞书退货登记表追过款(每日从tblCqER404qe57vV同步),追款清单排除'",
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
                for alter in ALTERS:
                    try:
                        cur.execute(alter)
                    except Exception as exc:
                        msg = str(exc)
                        if not any(x in msg for x in
                                   ("Duplicate column", "Duplicate key name",
                                    "check that column/key exists")):
                            raise
            conn.commit()
            print("profit_control schema OK: profit_cell_daily / return_case(+sale,income_actual) / issue_log / action_log")
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
