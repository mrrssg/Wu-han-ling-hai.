# -*- coding: utf-8 -*-
"""产品安全防控建表（幂等）。"""
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import create_app
from app.models.db_manager import DBManager

DDLS = [
    """CREATE TABLE IF NOT EXISTS order_system.safety_case (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        case_type VARCHAR(16) NOT NULL COMMENT '侵权/Prop65/危险品/召回/其它',
        title VARCHAR(255) NOT NULL,
        supplier VARCHAR(16) DEFAULT 'Costway',
        case_text TEXT,
        supplier_skus TEXT COMMENT '逗号分隔,含Excel解析出的',
        files_json TEXT COMMENT '[{name,path}]',
        fingerprint_json TEXT COMMENT 'Phase2 AI风险指纹',
        status VARCHAR(16) DEFAULT 'active' COMMENT 'active/closed',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        KEY idx_status (status)
    ) CHARSET=utf8mb4 COMMENT='产品安全案例库'""",
    """CREATE TABLE IF NOT EXISTS order_system.safety_product_cache (
        supplier VARCHAR(16) NOT NULL,
        sku VARCHAR(64) NOT NULL,
        title VARCHAR(400),
        spec TEXT,
        descr TEXT,
        image_url VARCHAR(600),
        synced_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (supplier, sku)
    ) CHARSET=utf8mb4 COMMENT='安全扫描用产品文本缓存(飞书供应商资料,只缓存我们目录内SKU)'""",
    """CREATE TABLE IF NOT EXISTS order_system.safety_hit (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        case_id BIGINT NOT NULL,
        supplier_sku VARCHAR(64),
        source_sku VARCHAR(64) COMMENT '触发它的案例SKU(家族展开来源)',
        hit_type VARCHAR(16) COMMENT 'direct直接命中/family同款家族/ai相似(Phase2)',
        platform VARCHAR(32),
        shop_name VARCHAR(64),
        shop_sku VARCHAR(64),
        active TINYINT COMMENT '1在卖/0停卖/NULL仅映射记录',
        orders_90d INT DEFAULT 0,
        risk_level VARCHAR(8) DEFAULT 'high',
        reason VARCHAR(512),
        status VARCHAR(16) DEFAULT 'open' COMMENT 'open/delisted/false_positive',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uq_hit (case_id, platform, shop_name, shop_sku, supplier_sku),
        KEY idx_case (case_id), KEY idx_status (status)
    ) CHARSET=utf8mb4 COMMENT='安全案例命中清单'""",
]


ALTERS = [
    "ALTER TABLE order_system.safety_product_cache ADD COLUMN category VARCHAR(160) DEFAULT NULL",
    "ALTER TABLE order_system.safety_product_cache ADD COLUMN in_catalog TINYINT DEFAULT 1 "
    "COMMENT '1=我们目录内(在卖/卖过) 0=供应商全量feed里我们没卖的(选品预警用)'",
    "ALTER TABLE order_system.safety_case ADD COLUMN scan_status VARCHAR(24) DEFAULT NULL "
    "COMMENT 'fingerprint_ready/scanning/done/error'",
    "ALTER TABLE order_system.safety_case ADD COLUMN scan_summary_json TEXT",
    "ALTER TABLE order_system.safety_hit ADD COLUMN evidence VARCHAR(1000) DEFAULT NULL "
    "COMMENT 'AI判定引用的产品原文'",
]


def main() -> int:
    app = create_app(os.environ.get("FLASK_CONFIG", "production"))
    with app.app_context():
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cur:
                for ddl in DDLS:
                    cur.execute(ddl)
                for alter in ALTERS:
                    try:
                        cur.execute(alter)
                    except Exception as exc:
                        if "Duplicate column" not in str(exc):
                            raise
            conn.commit()
            print("safety schema OK")
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
