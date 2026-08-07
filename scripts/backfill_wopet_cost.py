# -*- coding: utf-8 -*-
"""wopet 净利率成本回填。

飞书 wopet Mirakl 表(tbla2i1OwdwlCweK)「Shop SKU → 成本」→ order_system.macy_sku_cost(store='wopet')。
选品类目推荐分(_compute_macy_cat_demand)优先读它。不写共享的 offerprice_listing。
每天选品重建前跑(selection_rebuild_daily 已调用)。也可单独 `python scripts/backfill_wopet_cost.py`。
"""
import os
import sys
from pathlib import Path

import requests

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import create_app
from app.models.db_manager import DBManager

APP_ID = "cli_a940a2a1067adbd2"
SECRET = "i2mKLGVzUDmu4v0U9HYEYdMGc0ZvZAgU"
APP = "QEeubiXYGa83zXs3Zt8cSSJPnih"
TBL = "tbla2i1OwdwlCweK"   # Macy-wopet-Mirakl


def _gt(v):
    if isinstance(v, str):
        return v
    if isinstance(v, list) and v and isinstance(v[0], dict):
        return "".join(x.get("text", "") for x in v)
    if isinstance(v, dict):
        return v.get("text") or ""
    return str(v) if v is not None else ""


def _fetch_costs():
    """飞书 wopet 表 → {shop_sku: cost}(成本>0 的行)。"""
    tok = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": APP_ID, "app_secret": SECRET}, timeout=30,
    ).json()["tenant_access_token"]
    headers = {"Authorization": f"Bearer {tok}"}
    out, pt = {}, ""
    while True:
        url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP}/tables/{TBL}/records"
               f"?page_size=500" + (f"&page_token={pt}" if pt else ""))
        d = (requests.get(url, headers=headers, timeout=60).json().get("data") or {})
        for it in d.get("items") or []:
            f = it["fields"]
            sku = (_gt(f.get("Shop SKU")).strip() or _gt(f.get("店铺SKU")).strip())
            try:
                cost = float(f.get("成本"))
            except (TypeError, ValueError):
                cost = None
            if sku and cost and cost > 0:
                out[sku] = round(cost, 4)
        if not d.get("has_more"):
            break
        pt = d.get("page_token") or ""
        if not pt:
            break
    return out


def backfill_wopet_cost():
    """回填 macy_sku_cost(store='wopet')。返回写入条数。可在 app_context 内调用。"""
    costs = _fetch_costs()
    if not costs:
        print("[wopet_cost] 飞书没取到成本,跳过")
        return 0
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            items = list(costs.items())
            for i in range(0, len(items), 500):
                chunk = items[i:i + 500]
                ph = ",".join(["('wopet',%s,%s)"] * len(chunk))
                flat = [v for sku, cost in chunk for v in (sku, cost)]
                cur.execute(
                    "INSERT INTO order_system.macy_sku_cost (store, shop_sku, cost) VALUES " + ph +
                    " ON DUPLICATE KEY UPDATE cost=VALUES(cost)", flat)
        conn.commit()
    finally:
        conn.close()
    print(f"[wopet_cost] 飞书成本 {len(costs)} 条 → macy_sku_cost")
    return len(costs)


def main():
    app = create_app(os.environ.get("FLASK_CONFIG", "production"))
    with app.app_context():
        n = backfill_wopet_cost()
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) c FROM order_system.macy_sku_cost WHERE store='wopet'")
                total = cur.fetchone()["c"]
        finally:
            conn.close()
        print(f"[wopet_cost] 表内 wopet 成本共 {total} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
