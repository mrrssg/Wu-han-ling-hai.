# -*- coding: utf-8 -*-
"""Macy选品·类目汇聚+关键词粗筛（2026-07-23）。
把两个供应商所有类目（库存>50且没上过的产品所属的）灌进 macy_cat_map，
关键词粗筛：类目路径完全不含Macy能上类目的关键词 → 直接判无匹配(decided_by=prefilter)，
沾边的留 macy_leaf=NULL 等AI精判。"""
import os
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import create_app
from app.models.db_manager import DBManager

# Macy-Kuyotq能上的类目关键词（家具/家电/户外/宠物/存储等）——粗筛沾边判定
MACY_KEYWORDS = {
    "furniture", "chair", "table", "desk", "stool", "sofa", "bench", "cabinet",
    "dresser", "nightstand", "bed", "headboard", "crib", "bookcase", "shelf",
    "shelving", "tv stand", "media", "console", "room divider", "vanity", "island",
    "bar", "dining", "gaming", "office", "filing", "entryway", "wardrobe", "armoire",
    "outdoor", "patio", "garden", "porch", "swing", "fire pit", "gazebo", "pergola",
    "grill", "umbrella", "conversation set", "outdoor storage", "deck",
    "fan", "air conditioner", "air purifier", "humidifier", "heater",
    "pet", "cat", "dog", "animal", "kennel", "crate", "aquarium",
    "storage", "bin", "basket", "organization", "cart", "cover", "slip cover",
    "mattress", "bed frame",
}


def _feishu_used_skus():
    """全量拉 Macy-kuyotq-Mirakl + Macy-wopet-Mirakl 两表的「供应商SKU」。"""
    import json
    import requests
    APP_ID = "cli_a940a2a1067adbd2"
    SECRET = "i2mKLGVzUDmu4v0U9HYEYdMGc0ZvZAgU"
    APP = "QEeubiXYGa83zXs3Zt8cSSJPnih"
    tok = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": APP_ID, "app_secret": SECRET}, timeout=30
    ).json()["tenant_access_token"]
    H = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}

    def gt(v):
        if isinstance(v, str):
            return v
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return "".join(x.get("text", "") for x in v)
        if isinstance(v, dict):
            return v.get("text") or ""
        return str(v) if v is not None else ""

    used = set()
    for tbl in ("tblfyStm2eu3hp1Q", "tbla2i1OwdwlCweK"):
        page_token = ""
        while True:
            url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP}"
                   f"/tables/{tbl}/records?page_size=500"
                   + (f"&page_token={page_token}" if page_token else ""))
            r = requests.get(url, headers=H, timeout=60).json()
            data = r.get("data") or {}
            for it in data.get("items") or []:
                s = gt(it["fields"].get("供应商SKU")).strip()
                if s:
                    used.add(s)
            if not data.get("has_more"):
                break
            page_token = data.get("page_token") or ""
            if not page_token:
                break
    return used


def main() -> int:
    app = create_app(os.environ.get("FLASK_CONFIG", "production"))
    with app.app_context():
        # 已上过的供应商SKU = 飞书两张Mirakl表的「供应商SKU」全集（权威口径,用户定）
        used = _feishu_used_skus()
        print("Macy已上过供应商SKU(飞书两表):", len(used))
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cur:
                # 已上过SKU灌临时表(多值INSERT,防RDS慢)
                cur.execute("CREATE TEMPORARY TABLE _used_sku "
                            "(sku VARCHAR(64) COLLATE utf8mb4_general_ci PRIMARY KEY)")
                used_list = [s[:64] for s in used if s]
                for i in range(0, len(used_list), 2000):
                    chunk = used_list[i:i + 2000]
                    ph = ",".join(["(%s)"] * len(chunk))
                    cur.execute(f"INSERT IGNORE INTO _used_sku (sku) VALUES {ph}", chunk)
                # Costway类目 → 库存>50且没上过的产品数
                cur.execute("""
                    SELECT c.category AS cat, COUNT(*) AS n
                    FROM order_system.safety_product_cache c
                    JOIN autooperate.newestdropship d ON d.SKU=c.sku
                    LEFT JOIN _used_sku u ON u.sku=c.sku COLLATE utf8mb4_general_ci
                    WHERE c.supplier='Costway' AND c.category<>'' AND d.Stock>50
                      AND u.sku IS NULL
                    GROUP BY c.category""")
                cw = [(r["cat"], r["n"]) for r in cur.fetchall()]

                cur.execute("""
                    SELECT v.product_type AS cat, COUNT(*) AS n
                    FROM autooperate.vevor_feed v
                    LEFT JOIN _used_sku u ON u.sku=v.sku COLLATE utf8mb4_general_ci
                    WHERE v.product_type<>'' AND v.inventory>50 AND u.sku IS NULL
                    GROUP BY v.product_type""")
                vv = [(r["cat"], r["n"]) for r in cur.fetchall()]
        finally:
            conn.close()

        def sniff(cat):
            low = cat.lower()
            return any(kw in low for kw in MACY_KEYWORDS)

        rows = []
        for supplier, cats in (("Costway", cw), ("Vevor", vv)):
            for cat, n in cats:
                if not cat:
                    continue
                if sniff(cat):
                    rows.append((supplier, cat, n, None, "sniff", None))   # 待AI
                else:
                    rows.append((supplier, cat, n, None, "prefilter", "关键词不沾Macy可上类目"))

        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cur:
                for i in range(0, len(rows), 500):
                    chunk = rows[i:i + 500]
                    ph = ",".join(["(%s,%s,%s,%s,%s,%s)"] * len(chunk))
                    flat = [v for r in chunk for v in r]
                    cur.execute(f"""INSERT INTO order_system.macy_cat_map
                        (supplier, supplier_cat, product_count, macy_leaf, decided_by, ai_reason)
                        VALUES {ph}
                        ON DUPLICATE KEY UPDATE product_count=VALUES(product_count),
                          decided_by=IF(locked=1, decided_by, VALUES(decided_by)),
                          ai_reason=IF(locked=1, ai_reason, VALUES(ai_reason))""", flat)
            conn.commit()
            with conn.cursor() as cur:
                cur.execute("""SELECT decided_by, COUNT(*) n, SUM(product_count) p
                               FROM order_system.macy_cat_map GROUP BY decided_by""")
                for r in cur.fetchall():
                    print(f"  {r['decided_by']}: {r['n']}类目, {r['p']}产品")
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
