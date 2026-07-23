# -*- coding: utf-8 -*-
"""Lowes 选品（Autool=豪雅 / Yasonic=司顺）：类目/映射/候选池/推送记录表（幂等）。

与 Macy 选品同构，区别：
- 两个店共用同一套 Lowes 全类目（tblqP5459R0Lq7ua），只是能上的供应商不同
- 类目树很大（几千叶子），AI 映射两步走（先一级、再叶子），故 lowes_cat_map 多存 lowes_l1
- 候选池/推送记录带 store 字段区分 autool/yasonic，「每次只重建一个店铺」
"""
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import create_app
from app.models.db_manager import DBManager

DDLS = [
    # Lowes 全类目（两店共用）；full_path 由 Level1-4 拼成（飞书「完整路径」字段是截断的，不能用）
    """CREATE TABLE IF NOT EXISTS order_system.lowes_leaf_category (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        l1 VARCHAR(120) COMMENT '一级(42个之一)',
        l2 VARCHAR(120), l3 VARCHAR(120), l4 VARCHAR(120),
        leaf VARCHAR(120) COMMENT '最深一级(叶子)',
        full_path VARCHAR(400) NOT NULL COMMENT 'L1/L2/L3(/L4) 完整路径=店铺类目值',
        active TINYINT DEFAULT 1,
        UNIQUE KEY uq_path (full_path(300)),
        KEY idx_l1 (l1)
    ) CHARSET=utf8mb4 COMMENT='Lowes全类目(两店共用,店铺类目取full_path)'""",
    # 供应商类目 → Lowes 叶子映射（AI两步判+人工可锁定）
    """CREATE TABLE IF NOT EXISTS order_system.lowes_cat_map (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        supplier VARCHAR(16) NOT NULL COMMENT 'Costway(→Autool)/Vevor(→Yasonic)',
        supplier_cat VARCHAR(400) NOT NULL COMMENT '供应商类目(层级路径)',
        product_count INT DEFAULT 0 COMMENT '该类目库存>50且没上过的产品数',
        lowes_l1 VARCHAR(120) DEFAULT NULL COMMENT 'AI第一步:归到的一级;NULL=无匹配',
        lowes_leaf VARCHAR(120) DEFAULT NULL COMMENT 'AI第二步:叶子名',
        lowes_path VARCHAR(400) DEFAULT NULL COMMENT '完整路径=写店铺类目;NULL=无匹配',
        decided_by VARCHAR(12) DEFAULT NULL COMMENT 'prefilter/ai_l1/ai/manual',
        ai_reason VARCHAR(400),
        locked TINYINT DEFAULT 0 COMMENT '人工锁定,不被AI覆盖',
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uq_cat (supplier, supplier_cat(255)),
        KEY idx_path (lowes_leaf), KEY idx_l1 (lowes_l1)
    ) CHARSET=utf8mb4 COMMENT='供应商类目→Lowes叶子映射(AI两步,人工可改)'""",
    # 候选池（带 store，两店共存；重建只删当前店的行）
    """CREATE TABLE IF NOT EXISTS order_system.lowes_selection_pool (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        store VARCHAR(12) NOT NULL COMMENT 'autool/yasonic',
        supplier VARCHAR(16),
        supplier_sku VARCHAR(64),
        title VARCHAR(400),
        image VARCHAR(600),
        stock INT,
        supplier_cat VARCHAR(400),
        lowes_leaf VARCHAR(120),
        lowes_path VARCHAR(400) COMMENT '完整Lowes路径=店铺类目',
        brand VARCHAR(32) COMMENT 'autool=Volenca / yasonic=Mecale',
        price VARCHAR(32),
        heat_90d INT DEFAULT 0,
        has_overview_img TINYINT DEFAULT 0 COMMENT '图片总览表tbl2IRXCLuiUBfk9里有此SKU的图',
        rebuilt_at DATETIME,
        UNIQUE KEY uq_sku (store, supplier_sku),
        KEY idx_store (store), KEY idx_leaf (lowes_leaf)
    ) CHARSET=utf8mb4 COMMENT='Lowes选品候选池(每店独立,单店重建)'""",
    # 推送记录（带 store）
    """CREATE TABLE IF NOT EXISTS order_system.lowes_push_log (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        store VARCHAR(12) NOT NULL,
        batch_desc VARCHAR(255), sku_count INT,
        leaf_summary VARCHAR(1000) COMMENT '类目×数量',
        pushed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        KEY idx_store_at (store, pushed_at)
    ) CHARSET=utf8mb4 COMMENT='Lowes选品推送记录'""",
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
            print("lowes_selection schema OK")
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
