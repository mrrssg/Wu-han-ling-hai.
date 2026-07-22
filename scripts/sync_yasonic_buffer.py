# -*- coding: utf-8 -*-
"""Yasonic 上溢buffer同步（2026-07-22）：
给 offer_pricing_config 加 cost_buffer 列（幂等），从飞书 Lowes-Yasonic-Mirakl 表按
buffer = 成本 ÷ (供应商价格 × 0.8 × 1.07) 逐SKU反推真实上溢值，更新到配置表。
buffer 是每SKU固定的业务决策(1.05/1.08)，与实时供应商价无关，同步一次即可、定期刷。
"""
import json
import os
import sys
from pathlib import Path

import requests

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import create_app
from app.models.db_manager import DBManager

FEISHU_APP_ID = "cli_a940a2a1067adbd2"
FEISHU_APP_SECRET = "i2mKLGVzUDmu4v0U9HYEYdMGc0ZvZAgU"
PRODUCT_APP = "QEeubiXYGa83zXs3Zt8cSSJPnih"
YASONIC_TBL = "tbldeuRJOoJBfX2g"
REAL_COST_FACTOR = 0.8 * 1.07   # 0.856


def _gt(v):
    if isinstance(v, str):
        return v
    if isinstance(v, list) and v and isinstance(v[0], dict):
        return "".join(x.get("text", "") for x in v)
    if isinstance(v, dict):
        val = v.get("value")
        if isinstance(val, list) and val:
            return str(val[0])
        return v.get("text") or ""
    return str(v) if v is not None else ""


def _gn(v):
    try:
        if isinstance(v, dict):
            val = v.get("value")
            if isinstance(val, list) and val:
                return float(val[0])
        return float(_gt(v).replace("$", "").replace(",", ""))
    except (TypeError, ValueError):
        return None


def main() -> int:
    app = create_app(os.environ.get("FLASK_CONFIG", "production"))
    with app.app_context():
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cur:
                try:
                    cur.execute("ALTER TABLE order_system.offer_pricing_config "
                                "ADD COLUMN cost_buffer DECIMAL(6,4) DEFAULT NULL "
                                "COMMENT 'Yasonic上溢buffer(1.05/1.08),成本÷(供应商价×0.856)'")
                except Exception as exc:
                    if "Duplicate column" not in str(exc):
                        raise
            conn.commit()
        finally:
            conn.close()

        # 拉飞书 Yasonic 表全量，算 buffer
        tok = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
            timeout=30).json()["tenant_access_token"]
        H = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
        rows = []
        page_token = ""
        while True:
            url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{PRODUCT_APP}"
                   f"/tables/{YASONIC_TBL}/records?page_size=500"
                   + (f"&page_token={page_token}" if page_token else ""))
            r = requests.get(url, headers=H, timeout=60).json()
            data = r.get("data") or {}
            for it in data.get("items") or []:
                f = it["fields"]
                wh = _gt(f.get("供应商SKU")).strip()
                sp = _gn(f.get("供应商价格"))
                cost = _gn(f.get("成本"))
                if wh and sp and cost and sp > 0:
                    buffer = cost / (sp * REAL_COST_FACTOR)
                    # 只收合理范围(0.95~1.20)防脏数据；四舍五入到4位
                    if 0.95 <= buffer <= 1.20:
                        rows.append((round(buffer, 4), wh))
            if not data.get("has_more"):
                break
            page_token = data.get("page_token") or ""
            if not page_token:
                break

        conn = DBManager.get_connection()
        updated = 0
        try:
            with conn.cursor() as cur:
                for i in range(0, len(rows), 500):
                    for buf, wh in rows[i:i + 500]:
                        cur.execute(
                            "UPDATE order_system.offer_pricing_config SET cost_buffer=%s "
                            "WHERE store_key='lowes_yasonic' AND warehouse_sku=%s",
                            (buf, wh))
                        updated += cur.rowcount or 0
            conn.commit()
            # buffer 值分布抽查
            with conn.cursor() as cur:
                cur.execute("""SELECT cost_buffer, COUNT(*) n
                               FROM order_system.offer_pricing_config
                               WHERE store_key='lowes_yasonic' AND cost_buffer IS NOT NULL
                               GROUP BY cost_buffer ORDER BY n DESC LIMIT 10""")
                dist = cur.fetchall()
        finally:
            conn.close()
        print(f"feishu rows with buffer: {len(rows)}, config updated: {updated}")
        print("buffer分布:", [(str(r["cost_buffer"]), r["n"]) for r in dist])
    return 0


if __name__ == "__main__":
    sys.exit(main())
