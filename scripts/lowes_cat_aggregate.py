# -*- coding: utf-8 -*-
"""Lowes 选品·汇聚供应商类目（2026-07-23）。
把 Costway(→Autool) + Vevor(→Yasonic) 所有「库存>50 产品的类目」灌进 lowes_cat_map，
decided_by='sniff' 待 AI 两步精判。product_count=该类目库存>50 的产品数(排序AI用)。
不在这里排除已上过SKU：映射是类目级的，已上过排除放在 rebuild 时按店铺做。
Lowes 覆盖面广，不做关键词粗筛——由 AI 第一步"归不到42个一级之一"充当过滤。
"""
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import create_app
from app.models.db_manager import DBManager


def main() -> int:
    app = create_app(os.environ.get("FLASK_CONFIG", "production"))
    with app.app_context():
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT c.category AS cat, COUNT(*) AS n
                    FROM order_system.safety_product_cache c
                    JOIN autooperate.newestdropship d ON d.SKU=c.sku
                    WHERE c.supplier='Costway' AND c.category<>'' AND d.Stock>50
                    GROUP BY c.category""")
                cw = [(r["cat"], r["n"]) for r in cur.fetchall()]
                cur.execute("""
                    SELECT v.product_type AS cat, COUNT(*) AS n
                    FROM autooperate.vevor_feed v
                    WHERE v.product_type<>'' AND v.inventory>50
                    GROUP BY v.product_type""")
                vv = [(r["cat"], r["n"]) for r in cur.fetchall()]
        finally:
            conn.close()
        print(f"Costway类目:{len(cw)} Vevor类目:{len(vv)}")

        rows = []
        for supplier, cats in (("Costway", cw), ("Vevor", vv)):
            for cat, n in cats:
                if cat:
                    rows.append((supplier, cat[:400], n))

        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cur:
                for i in range(0, len(rows), 500):
                    chunk = rows[i:i + 500]
                    ph = ",".join(["(%s,%s,%s,'sniff')"] * len(chunk))
                    flat = [v for r in chunk for v in r]
                    # 已存在的：只更新数量，不覆盖已判结果(decided_by/lowes_*)，锁定的更不动
                    cur.execute(f"""INSERT INTO order_system.lowes_cat_map
                        (supplier, supplier_cat, product_count, decided_by) VALUES {ph}
                        ON DUPLICATE KEY UPDATE product_count=VALUES(product_count)""", flat)
            conn.commit()
            with conn.cursor() as cur:
                cur.execute("""SELECT decided_by, COUNT(*) n, COALESCE(SUM(product_count),0) p
                               FROM order_system.lowes_cat_map GROUP BY decided_by""")
                for r in cur.fetchall():
                    print(f"  {r['decided_by']}: {r['n']}类目 {r['p']}产品")
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
