# -*- coding: utf-8 -*-
"""P2 选品：豪雅 feed 新品/新库存识别（2026-08-06）。
- newestdropship 加 first_seen(首次出现feed日期) / restock_at(0→有货日期)，由导入器维护；
  存量行 first_seen 回填成远古日期，保证"新品"只从现在起识别、不回溯误判。
- lowes_selection_pool 加 is_new / is_restock 标记。
幂等：重复列忽略。
"""
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import create_app
from app.models.db_manager import DBManager

ALTERS = [
    "ALTER TABLE autooperate.newestdropship ADD COLUMN first_seen DATE DEFAULT NULL "
    "COMMENT '首次出现在feed的日期(新品判定);导入器维护,存量回填远古'",
    "ALTER TABLE autooperate.newestdropship ADD COLUMN restock_at DATE DEFAULT NULL "
    "COMMENT '最近一次库存0→>0的日期(新库存判定);导入器维护'",
    "ALTER TABLE order_system.lowes_selection_pool ADD COLUMN is_new TINYINT DEFAULT 0 "
    "COMMENT '豪雅新品(first_seen近N天)'",
    "ALTER TABLE order_system.lowes_selection_pool ADD COLUMN is_restock TINYINT DEFAULT 0 "
    "COMMENT '豪雅新库存(restock_at近N天)'",
    # 司顺(Yasonic)feed 同样加，由 sync_vevor_feed.py 维护
    "ALTER TABLE autooperate.vevor_feed ADD COLUMN first_seen DATE DEFAULT NULL "
    "COMMENT '首次出现在vevor_feed的日期(新品判定)'",
    "ALTER TABLE autooperate.vevor_feed ADD COLUMN restock_at DATE DEFAULT NULL "
    "COMMENT '最近一次inventory 0→>0的日期(新库存判定)'",
]


def main() -> int:
    app = create_app(os.environ.get("FLASK_CONFIG", "production"))
    with app.app_context():
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cur:
                for a in ALTERS:
                    try:
                        cur.execute(a)
                        print("OK:", a[:70])
                    except Exception as exc:
                        if "Duplicate column" in str(exc):
                            print("skip(exists):", a[:70])
                        else:
                            raise
                # 存量行 first_seen 回填远古(=不算新品)；只填 NULL 的
                cur.execute("UPDATE autooperate.newestdropship "
                            "SET first_seen='2025-01-01' WHERE first_seen IS NULL")
                print(f"backfilled newestdropship first_seen: {cur.rowcount}")
                cur.execute("UPDATE autooperate.vevor_feed "
                            "SET first_seen='2025-01-01' WHERE first_seen IS NULL")
                print(f"backfilled vevor_feed first_seen: {cur.rowcount}")
            conn.commit()
            print("costway newness schema OK")
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
