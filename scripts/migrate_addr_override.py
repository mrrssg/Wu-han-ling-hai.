# -*- coding: utf-8 -*-
"""客户改址覆盖表（幂等）。"""
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import create_app
from app.models.db_manager import DBManager

DDL = """
CREATE TABLE IF NOT EXISTS order_system.order_address_override (
    order_number VARCHAR(64) PRIMARY KEY COMMENT '平台订单号(与导出取数的order_number一致)',
    first_name VARCHAR(80), last_name VARCHAR(80), phone VARCHAR(40),
    street VARCHAR(255), city VARCHAR(80), region VARCHAR(40), postcode VARCHAR(16),
    note VARCHAR(255) COMMENT '改址原因备注',
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) CHARSET=utf8mb4 COMMENT='客户改址覆盖：供应商未发货导出时替换原地址(非空字段才覆盖)'
"""


def main() -> int:
    app = create_app(os.environ.get("FLASK_CONFIG", "production"))
    with app.app_context():
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(DDL)
            conn.commit()
            print("order_address_override schema OK")
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
