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
            print("macy_selection schema OK")
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
