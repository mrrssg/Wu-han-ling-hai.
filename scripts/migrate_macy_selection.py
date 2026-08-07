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
]

ALTERS = [
    "ALTER TABLE order_system.macy_selection_pool ADD COLUMN is_new TINYINT DEFAULT 0 "
    "COMMENT '新品(first_seen近14天,Costway/Vevor任一)'",
    "ALTER TABLE order_system.macy_selection_pool ADD COLUMN is_restock TINYINT DEFAULT 0 "
    "COMMENT '新补货(restock_at近14天)'",
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
                        if "Duplicate column" not in str(exc):
                            raise
            conn.commit()
            print("macy_selection schema OK")
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
