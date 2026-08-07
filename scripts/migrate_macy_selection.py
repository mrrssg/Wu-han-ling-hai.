# -*- coding: utf-8 -*-
"""Macy-Kuyotq 选品：类目映射表 + Macy叶子类目表（幂等）。"""
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import create_app
from app.models.db_manager import DBManager

DDLS = [
    # Macy 能上的叶子类目（从桌面Excel导入；带品牌+完整路径+categoryCode）
    """CREATE TABLE IF NOT EXISTS order_system.macy_leaf_category (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        brand VARCHAR(32) COMMENT 'Mecale/Ecooso/Volenca',
        leaf VARCHAR(120) NOT NULL COMMENT '叶子类目名(第4级,如Bar Stools)',
        full_path VARCHAR(300) COMMENT '完整Macy类目路径',
        category_code VARCHAR(120) COMMENT '上架用的categoryCode(有则填)',
        active TINYINT DEFAULT 1,
        UNIQUE KEY uq_leaf (brand, leaf)
    ) CHARSET=utf8mb4 COMMENT='Macy-Kuyotq能上的叶子类目清单'""",
    # 供应商类目 → Macy叶子类目 映射（AI判+人工可锁定）
    """CREATE TABLE IF NOT EXISTS order_system.macy_cat_map (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        supplier VARCHAR(16) NOT NULL COMMENT 'Costway/Vevor',
        supplier_cat VARCHAR(400) NOT NULL COMMENT '供应商类目(层级路径)',
        product_count INT DEFAULT 0 COMMENT '该类目库存>50且没上过的产品数',
        macy_leaf VARCHAR(120) DEFAULT NULL COMMENT '映射到的Macy叶子类目;NULL=无匹配',
        macy_brand VARCHAR(32) DEFAULT NULL,
        decided_by VARCHAR(12) DEFAULT NULL COMMENT 'prefilter/ai/manual',
        ai_reason VARCHAR(400),
        locked TINYINT DEFAULT 0 COMMENT '人工锁定,不被AI覆盖',
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uq_cat (supplier, supplier_cat(255)),
        KEY idx_leaf (macy_leaf)
    ) CHARSET=utf8mb4 COMMENT='供应商类目→Macy叶子类目映射(AI判,人工可改)'""",
    # 类目需求分（净利率×GMV,店铺参数化便于将来加其它Macy店）
    """CREATE TABLE IF NOT EXISTS order_system.macy_cat_demand (
        store VARCHAR(12) NOT NULL DEFAULT 'kuyotq',
        macy_leaf VARCHAR(120) NOT NULL,
        gmv DECIMAL(14,2) DEFAULT 0,
        units INT DEFAULT 0,
        margin_rate DECIMAL(6,4) DEFAULT NULL COMMENT '净利率=(收入-实际佣金-成本)/收入',
        gross_rate DECIMAL(6,4) DEFAULT NULL COMMENT '毛利率(1-成本/收入)',
        comm_rate DECIMAL(6,4) DEFAULT NULL COMMENT '实际佣金率',
        score INT DEFAULT 0 COMMENT '0~100:GMV与净利率加权',
        season_tag VARCHAR(24) DEFAULT NULL,
        season_peak VARCHAR(8) DEFAULT NULL,
        trend_now INT DEFAULT NULL,
        season_profile TEXT DEFAULT NULL,
        computed_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (store, macy_leaf)
    ) CHARSET=utf8mb4 COMMENT='Macy类目需求分(净利率×GMV,store参数化)'""",
    # 蓝海类目（未涉及：供应商有货但0销量；邻接适配+货盘+季节+Amazon需求）
    """CREATE TABLE IF NOT EXISTS order_system.macy_blue_ocean (
        store VARCHAR(12) NOT NULL DEFAULT 'kuyotq',
        macy_leaf VARCHAR(120) NOT NULL,
        l1 VARCHAR(120), l2 VARCHAR(120), l3 VARCHAR(120),
        brand VARCHAR(32),
        sku_n INT DEFAULT 0, with_img INT DEFAULT 0,
        avg_price DECIMAL(10,2) DEFAULT NULL, avg_stock INT DEFAULT NULL,
        fit_score INT DEFAULT 0 COMMENT '邻接适配0~100(同L3强/同L2中/无邻接弱)',
        fit_reason VARCHAR(255),
        supply_score INT DEFAULT 0,
        gt_keyword VARCHAR(120),
        season_tag VARCHAR(24) DEFAULT NULL, season_peak VARCHAR(8) DEFAULT NULL,
        trend_now INT DEFAULT NULL, season_profile TEXT DEFAULT NULL,
        amz_units INT DEFAULT NULL, amz_revenue DECIMAL(14,2) DEFAULT NULL,
        amz_price DECIMAL(10,2) DEFAULT NULL, amz_return DECIMAL(6,2) DEFAULT NULL,
        amz_node VARCHAR(120) DEFAULT NULL,
        blue_score INT DEFAULT 0,
        computed_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (store, macy_leaf),
        KEY idx_store_score (store, blue_score)
    ) CHARSET=utf8mb4 COMMENT='Macy蓝海类目推荐(邻接适配+季节+Amazon需求)'""",
    # 擦边池逐SKU人工决策：采用进精选(可改类目)/弃用；每日重建遵循,不被冲掉
    """CREATE TABLE IF NOT EXISTS order_system.macy_selection_decision (
        store VARCHAR(12) NOT NULL,
        supplier VARCHAR(16) NOT NULL,
        supplier_sku VARCHAR(64) NOT NULL,
        decision VARCHAR(12) NOT NULL DEFAULT 'approved' COMMENT 'approved采用进精选/rejected弃用',
        override_leaf VARCHAR(120) DEFAULT NULL COMMENT '人工改后的Macy叶子;NULL=用原判',
        override_brand VARCHAR(32) DEFAULT NULL,
        decided_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (store, supplier, supplier_sku)
    ) CHARSET=utf8mb4 COMMENT='擦边池人工决策(采用/弃用/改类目),每日重建遵循'""",
    # 人工锁定的 供应商类目→Macy叶子 覆盖(记住映射,同类含将来新品自动跟)——两店通用
    """CREATE TABLE IF NOT EXISTS order_system.macy_cat_override (
        store VARCHAR(12) NOT NULL,
        supplier VARCHAR(16) NOT NULL,
        supplier_cat VARCHAR(400) NOT NULL,
        override_leaf VARCHAR(120) NOT NULL,
        override_brand VARCHAR(32) DEFAULT NULL,
        note VARCHAR(200) DEFAULT NULL,
        decided_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (store, supplier, supplier_cat(200))
    ) CHARSET=utf8mb4 COMMENT='人工锁定 供应商类目→Macy叶子(记住映射,同类+将来新品自动跟)'""",
]

ALTERS = [
    "ALTER TABLE order_system.macy_selection_pool ADD COLUMN is_new TINYINT DEFAULT 0 "
    "COMMENT '新品(first_seen近14天,Costway/Vevor任一)'",
    "ALTER TABLE order_system.macy_selection_pool ADD COLUMN is_restock TINYINT DEFAULT 0 "
    "COMMENT '新补货(restock_at近14天)'",
    # 店铺参数化 + 两池(tier)
    "ALTER TABLE order_system.macy_selection_pool ADD COLUMN store VARCHAR(12) NOT NULL DEFAULT 'kuyotq' "
    "COMMENT '哪个Macy店(kuyotq/wopet…)'",
    "ALTER TABLE order_system.macy_selection_pool ADD COLUMN tier VARCHAR(8) NOT NULL DEFAULT 'ai' "
    "COMMENT 'ai=精选(有把握)/manual=人工待选(擦边)'",
    "ALTER TABLE order_system.macy_selection_pool ADD COLUMN classify_reason VARCHAR(200) DEFAULT NULL "
    "COMMENT '归类依据(命中的关键词/擦边原因)'",
    # 唯一键改成含 store(同一SKU可同时是不同店的候选)
    "ALTER TABLE order_system.macy_selection_pool DROP INDEX uq_sku",
    "ALTER TABLE order_system.macy_selection_pool ADD UNIQUE KEY uq_sku (store, supplier, supplier_sku)",
    # cat_demand / blue_ocean 已经 store 参数化(默认kuyotq),wopet 直接用 store='wopet'
]


def main() -> int:
    app = create_app(os.environ.get("FLASK_CONFIG", "production"))
    with app.app_context():
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cur:
                for ddl in DDLS:
                    cur.execute(ddl)
                for alt in ALTERS:
                    try:
                        cur.execute(alt)
                    except Exception as exc:
                        msg = str(exc)
                        if not any(k in msg for k in (
                                "Duplicate column", "Duplicate key", "check that column/key exists",
                                "Can't DROP", "already exists")):
                            raise
            conn.commit()
            print("macy_selection schema OK")
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
