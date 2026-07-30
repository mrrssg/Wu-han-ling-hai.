# -*- coding: utf-8 -*-
"""同步飞书【豪雅尺寸表】(tbl6NtZav7zrPHf3) → order_system.costway_box_dims。

cpbh(产品编号,含多箱后缀如 HW71816-14/-24/-34/-44) → 长/宽/高(in) + 净重(lb)。
退货把关工具算"原始尺寸退货运费"时,warehouse_sku 按 "+" 拆成组件码,
每个码在此表查(精确码=单箱;码-箱号=多箱各箱),逐箱算运费加总。

认证复用 instance/feishu_app.json(app_id/app_secret)。约4.9万条,GET分页同步。
用法: PYTHONPATH=... FLASK_CONFIG=production ./venv/bin/python scripts/sync_costway_dims.py
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
from app import create_app
from app.models.db_manager import DBManager

APP_TOKEN = "QEeubiXYGa83zXs3Zt8cSSJPnih"
TABLE_ID = "tbl6NtZav7zrPHf3"
BASE = "https://open.feishu.cn/open-apis"

DDL = """
CREATE TABLE IF NOT EXISTS order_system.costway_box_dims (
  cpbh VARCHAR(80) NOT NULL PRIMARY KEY,
  l_in DECIMAL(7,2), w_in DECIMAL(7,2), h_in DECIMAL(7,2), weight_lb DECIMAL(9,3),
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def _creds():
    base = os.environ.get("BASE_DIR") or str(Path(__file__).resolve().parent.parent)
    with open(os.path.join(base, "instance", "feishu_app.json"), encoding="utf-8") as f:
        d = json.load(f)
    return d["app_id"].strip(), d["app_secret"].strip()


def _token():
    aid, sec = _creds()
    r = requests.post(f"{BASE}/auth/v3/tenant_access_token/internal",
                      json={"app_id": aid, "app_secret": sec}, timeout=20).json()
    if r.get("code") != 0:
        raise RuntimeError(f"auth failed: {r}")
    return r["tenant_access_token"]


def _cell(v):
    if isinstance(v, list):
        return (v[0].get("text") if v and isinstance(v[0], dict) else (str(v[0]) if v else None))
    return v


def _num(v):
    v = _cell(v)
    try:
        return round(float(v), 3) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def fetch_all(tok):
    H = {"Authorization": f"Bearer {tok}"}
    rows, page_token = [], None
    while True:
        params = {"page_size": 500,
                  "field_names": json.dumps(["cpbh", "长(in)", "宽(in)", "高(in)", "净重(lb)"],
                                            ensure_ascii=False)}
        if page_token:
            params["page_token"] = page_token
        r = requests.get(f"{BASE}/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/records",
                         headers=H, params=params, timeout=40).json()
        if r.get("code") != 0:
            raise RuntimeError(f"fetch failed: {r.get('code')} {r.get('msg')}")
        data = r.get("data") or {}
        for it in data.get("items") or []:
            f = it.get("fields") or {}
            cpbh = _cell(f.get("cpbh"))
            if not cpbh:
                continue
            rows.append((str(cpbh).strip(), _num(f.get("长(in)")), _num(f.get("宽(in)")),
                         _num(f.get("高(in)")), _num(f.get("净重(lb)"))))
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
        if not page_token:
            break
        if len(rows) % 5000 < 500:
            print(f"  ...{len(rows)} 条")
    return rows


def main():
    app = create_app(os.environ.get("FLASK_CONFIG", "production"))
    with app.app_context():
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(DDL)
            conn.commit()
            t0 = time.time()
            rows = fetch_all(_token())
            print(f"拉到 {len(rows)} 条,耗时 {time.time()-t0:.0f}s,开始写库...")
            # 去重(同cpbh取最后一条)
            uniq = {}
            for r in rows:
                uniq[r[0]] = r
            rows = list(uniq.values())
            sql = ("INSERT INTO order_system.costway_box_dims (cpbh,l_in,w_in,h_in,weight_lb) "
                   "VALUES (%s,%s,%s,%s,%s) "
                   "ON DUPLICATE KEY UPDATE l_in=VALUES(l_in),w_in=VALUES(w_in),"
                   "h_in=VALUES(h_in),weight_lb=VALUES(weight_lb)")
            with conn.cursor() as cur:
                CH = 1000
                for i in range(0, len(rows), CH):
                    cur.executemany(sql, rows[i:i + CH])
                conn.commit()
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) n FROM order_system.costway_box_dims")
                n = cur.fetchone()["n"]
            print(f"完成: costway_box_dims 现有 {n} 条(去重后写入 {len(rows)})")
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
