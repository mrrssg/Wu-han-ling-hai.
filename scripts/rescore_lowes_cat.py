# -*- coding: utf-8 -*-
"""重算 Lowes 类目净利率推荐分并回刷到候选池 heat_90d（不重读飞书、不重建池）。
用于口径调整后立刻生效：_compute_category_demand 重写 lowes_cat_demand(净利率口径)，
再用池里已存的 is_new/is_restock 重算每行 heat_90d = 类目分 + 新品/新补货加成。"""
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import create_app
from app.models.db_manager import DBManager
from app.services.lowes_selection_service import (
    _compute_category_demand, NEW_BONUS, RESTOCK_BONUS,
)


def main() -> int:
    app = create_app(os.environ.get("FLASK_CONFIG", "production"))
    with app.app_context():
        for store in ("autool", "yasonic"):
            conn = DBManager.get_connection()
            try:
                with conn.cursor() as cur:
                    scores = _compute_category_demand(cur, store)
                    cur.execute("SELECT id, lowes_leaf, is_new, is_restock "
                                "FROM order_system.lowes_selection_pool WHERE store=%s", (store,))
                    pool = cur.fetchall()
                    ups = []
                    for p in pool:
                        base = scores.get(p["lowes_leaf"] or "", 0)
                        bonus = NEW_BONUS if p["is_new"] else (RESTOCK_BONUS if p["is_restock"] else 0)
                        ups.append((min(100, base + bonus), p["id"]))
                    if ups:
                        cur.executemany(
                            "UPDATE order_system.lowes_selection_pool SET heat_90d=%s WHERE id=%s", ups)
                conn.commit()
                print(f"{store}: cats={len(scores)} pool_rows={len(pool)} rescored")
            finally:
                conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
