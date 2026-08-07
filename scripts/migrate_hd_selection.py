# -*- coding: utf-8 -*-
"""HD(Home Depot)选品建表(幂等)。TOP=厨卫/小家电、BOS=户外/庭院,两店同平台跨店去重。"""
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import create_app
from app.models.db_manager import DBManager

DDLS = [
    # 各店可上的HD类目白名单(从桌面Excel「产品类目去重」)
    """CREATE TABLE IF NOT EXISTS order_system.hd_leaf_category (
        store VARCHAR(8) NOT NULL COMMENT 'top/bos',
        hd_path VARCHAR(400) NOT NULL COMMENT '完整HD类目路径(店铺类目)',
        product_count INT DEFAULT 0 COMMENT 'Excel里现有产品数(参考)',
        active TINYINT DEFAULT 1,
        PRIMARY KEY (store, hd_path(255))
    ) CHARSET=utf8mb4 COMMENT='HD各店可上类目白名单(TOP84/BOS100)'""",
    # 供应商类目 → HD店铺类目 映射(从现有飞书记录抽 + 人工/未归类桶补)
    """CREATE TABLE IF NOT EXISTS order_system.hd_cat_map (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        store VARCHAR(8) NOT NULL,
        supplier VARCHAR(16) NOT NULL COMMENT 'Costway/Vevor',
        supplier_cat VARCHAR(400) NOT NULL,
        hd_path VARCHAR(400) DEFAULT NULL COMMENT '映射到的HD类目;NULL=无匹配',
        tier VARCHAR(8) DEFAULT 'ai' COMMENT 'record一致=精选/conflict多落点=擦边',
        decided_by VARCHAR(12) DEFAULT NULL COMMENT 'record/ai/manual',
        ai_reason VARCHAR(400),
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uq (store, supplier, supplier_cat(200)),
        KEY idx_path (hd_path(120))
    ) CHARSET=utf8mb4 COMMENT='HD供应商类目→店铺类目映射(现有记录抽+人工补)'""",
    # 候选池
    """CREATE TABLE IF NOT EXISTS order_system.hd_selection_pool (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        store VARCHAR(8) NOT NULL,
        tier VARCHAR(8) NOT NULL DEFAULT 'ai' COMMENT 'ai精选/manual擦边',
        classify_reason VARCHAR(200) DEFAULT NULL,
        supplier VARCHAR(16), supplier_sku VARCHAR(64),
        title VARCHAR(500), image VARCHAR(600), stock INT,
        supplier_cat VARCHAR(400), hd_path VARCHAR(400), brand VARCHAR(48),
        price VARCHAR(32), heat_90d INT DEFAULT 0,
        has_overview_img TINYINT DEFAULT 0, is_new TINYINT DEFAULT 0, is_restock TINYINT DEFAULT 0,
        rebuilt_at DATETIME DEFAULT NULL,
        UNIQUE KEY uq_sku (store, supplier, supplier_sku),
        KEY idx_store_tier (store, tier)
    ) CHARSET=utf8mb4 COMMENT='HD选品候选池'""",
    # 已上过SKU快照(未归类桶排除已上过)
    """CREATE TABLE IF NOT EXISTS order_system.hd_used_sku (
        store VARCHAR(8) NOT NULL,
        supplier_sku VARCHAR(64) NOT NULL,
        PRIMARY KEY (store, supplier_sku)
    ) CHARSET=utf8mb4 COMMENT='HD已上过供应商SKU快照(每次重建刷新)'""",
    # 擦边池逐SKU决策
    """CREATE TABLE IF NOT EXISTS order_system.hd_selection_decision (
        store VARCHAR(8) NOT NULL,
        supplier VARCHAR(16) NOT NULL,
        supplier_sku VARCHAR(64) NOT NULL,
        decision VARCHAR(12) NOT NULL DEFAULT 'approved' COMMENT 'approved采用/rejected弃用',
        override_leaf VARCHAR(400) DEFAULT NULL COMMENT '人工改后的HD类目',
        override_brand VARCHAR(48) DEFAULT NULL,
        decided_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (store, supplier, supplier_sku)
    ) CHARSET=utf8mb4 COMMENT='HD擦边池人工决策'""",
    # 人工锁定 供应商类目→HD类目 覆盖(记住映射)
    """CREATE TABLE IF NOT EXISTS order_system.hd_cat_override (
        store VARCHAR(8) NOT NULL,
        supplier VARCHAR(16) NOT NULL,
        supplier_cat VARCHAR(400) NOT NULL,
        override_leaf VARCHAR(400) NOT NULL,
        override_brand VARCHAR(48) DEFAULT NULL,
        note VARCHAR(200) DEFAULT NULL,
        decided_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (store, supplier, supplier_cat(200))
    ) CHARSET=utf8mb4 COMMENT='HD人工锁定供应商类目→HD类目'""",
    # 本地已推镜像(补飞书同步延迟)
    """CREATE TABLE IF NOT EXISTS order_system.hd_pushed_sku (
        supplier_sku VARCHAR(64) NOT NULL,
        store VARCHAR(8) NOT NULL,
        pushed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (supplier_sku, store)
    ) CHARSET=utf8mb4 COMMENT='HD已推SKU本地镜像'""",
    # 推送日志
    """CREATE TABLE IF NOT EXISTS order_system.hd_push_log (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        store VARCHAR(8), batch_desc VARCHAR(200), sku_count INT,
        costway_n INT, vevor_n INT, leaf_summary VARCHAR(500),
        pushed_at DATETIME DEFAULT CURRENT_TIMESTAMP
    ) CHARSET=utf8mb4 COMMENT='HD推送日志'""",
]


def main() -> int:
    app = create_app(os.environ.get("FLASK_CONFIG", "production"))
    with app.app_context():
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cur:
                for ddl in DDLS:
                    cur.execute(ddl)
            conn.commit()
            print("hd_selection schema OK")
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
