# -*- coding: utf-8 -*-
"""HD 白名单 + 类目映射 seed。

1. 桌面Excel白名单(/tmp/hd_whitelist.json) → hd_leaf_category
2. 从两张HD飞书表现有记录抽 (供应商, 供应商类目) → 店铺类目(HD格式) → hd_cat_map
   同一供应商类目落点一致=record(精选);落多个HD类目=conflict(擦边,取最常见)
"""
import json
import os
import sys
from collections import Counter, defaultdict
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
HD_TABLES = {"top": "tblxHsORDrH6Ldvr", "bos": "tbl4OAnBZliXZ0Lm"}
WHITELIST_JSON = "/tmp/hd_whitelist.json"


def _gt(v):
    if isinstance(v, str):
        return v
    if isinstance(v, list) and v and isinstance(v[0], dict):
        return "".join(x.get("text", "") for x in v)
    if isinstance(v, dict):
        return v.get("text") or ""
    return str(v) if v is not None else ""


def _token():
    return requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": APP_ID, "app_secret": SECRET}, timeout=30,
    ).json()["tenant_access_token"]


def _read_records(tbl, headers):
    out, pt = [], ""
    while True:
        url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP}/tables/{tbl}/records"
               f"?page_size=500" + (f"&page_token={pt}" if pt else ""))
        d = (requests.get(url, headers=headers, timeout=60).json().get("data") or {})
        for it in d.get("items") or []:
            f = it["fields"]
            out.append((_gt(f.get("供应商")).strip(),
                        _gt(f.get("供应商类目")).strip(),
                        _gt(f.get("店铺类目")).strip()))
        if not d.get("has_more"):
            break
        pt = d.get("page_token") or ""
        if not pt:
            break
    return out


def main():
    app = create_app(os.environ.get("FLASK_CONFIG", "production"))
    with app.app_context():
        conn = DBManager.get_connection()
        # 1) 白名单
        wl = json.load(open(WHITELIST_JSON, encoding="utf-8"))
        with conn.cursor() as cur:
            cur.execute("DELETE FROM order_system.hd_leaf_category")
            for i in range(0, len(wl), 500):
                c = wl[i:i + 500]
                ph = ",".join(["(%s,%s,%s,1)"] * len(c))
                cur.execute("INSERT INTO order_system.hd_leaf_category (store,hd_path,product_count,active) VALUES " + ph,
                            [v for x in c for v in (x["store"], x["hd_path"], x["count"])])
        conn.commit()
        print(f"hd_leaf_category: {len(wl)} 条白名单")

        # 2) 类目映射 from 飞书记录
        H = {"Authorization": f"Bearer {_token()}"}
        # (store,supplier,supplier_cat) -> Counter(HD店铺类目)
        agg = defaultdict(Counter)
        for store, tbl in HD_TABLES.items():
            recs = _read_records(tbl, H)
            hd_fmt = 0
            for sup, scat, stcat in recs:
                if not (sup and scat):
                    continue
                if stcat.startswith("The Home Depot"):   # 只用HD格式的店铺类目
                    agg[(store, sup, scat)][stcat] += 1
                    hd_fmt += 1
            print(f"  {store}({tbl}): {len(recs)}条, HD格式店铺类目 {hd_fmt}条")

        rows, n_record, n_conflict = [], 0, 0
        for (store, sup, scat), cnt in agg.items():
            top_path, top_n = cnt.most_common(1)[0]
            if len(cnt) == 1:
                tier, decided, reason = "record", "record", f"现有{top_n}条一致"
                n_record += 1
            else:
                tier, decided, reason = "conflict", "record", f"多落点{dict(cnt)}取最常见"
                n_conflict += 1
            rows.append((store, sup, scat, top_path, tier, decided, reason[:400]))

        with conn.cursor() as cur:
            for store, sup, scat, path, tier, decided, reason in rows:
                cur.execute(
                    "INSERT INTO order_system.hd_cat_map "
                    "(store,supplier,supplier_cat,hd_path,tier,decided_by,ai_reason) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s) "
                    "ON DUPLICATE KEY UPDATE hd_path=VALUES(hd_path), tier=VALUES(tier), "
                    "decided_by=VALUES(decided_by), ai_reason=VALUES(ai_reason)",
                    (store, sup, scat, path, tier, decided, reason))
        conn.commit()
        conn.close()
        print(f"hd_cat_map: 共 {len(rows)} 条映射 (record一致={n_record} / conflict多落点={n_conflict})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
