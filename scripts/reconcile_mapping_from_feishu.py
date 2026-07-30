# -*- coding: utf-8 -*-
"""从飞书 *-Mirakl 表全量对账补 SKU 映射(绕开 offer 同步)。

用途:当 offer 同步落后/失败,但店铺已"通过"很多新 listing 时,直接从该店铺
飞书表拉全量 shop SKU,把不在 autooperate.mapping_table 的补进去。复用
mapping_backfill_service 的五道校验(绝不跨店/精确匹配/冲突跳过/供应商SKU须真实/
只补空缺),owner 按前缀判。只读飞书 + 只 INSERT 缺失,永不覆盖已有。

    python scripts/reconcile_mapping_from_feishu.py --store lowes_autool
"""
import argparse
import json
import sys

import requests

import app as app_module
from app.services.mapping_backfill_service import (
    FEISHU_APP, STORE_FEISHU, _gt, backfill_mapping_for_new_skus,
)


def _all_shop_skus(store_key: str) -> list:
    """分页拉该店铺飞书表全部 shop SKU(候选字段取第一个非空命中)。"""
    from app.services.listing_sentinel_service import _token
    tbl, fields = STORE_FEISHU[store_key]
    headers = {"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"}
    url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP}"
           f"/tables/{tbl}/records/search?page_size=500")
    skus, seen_field = [], None
    page_token = None
    while True:
        u = url + (f"&page_token={page_token}" if page_token else "")
        r = requests.post(u, headers=headers, data=json.dumps({}).encode("utf-8"),
                          timeout=30).json()
        data = r.get("data") or {}
        for it in data.get("items") or []:
            f = it.get("fields", {})
            for field in fields:
                v = _gt(f.get(field)).strip()
                if v:
                    skus.append(v)
                    seen_field = field
                    break
        page_token = data.get("page_token")
        if not data.get("has_more") or not page_token:
            break
    return skus, seen_field


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default="lowes_autool", choices=list(STORE_FEISHU))
    args = ap.parse_args()

    app = app_module.create_app()
    with app.app_context():
        skus, field = _all_shop_skus(args.store)
        uniq = sorted(set(skus))
        print(f"[{args.store}] 飞书表拉到 {len(skus)} 行 / 去重 {len(uniq)} 个 shop SKU (字段={field})")
        res = backfill_mapping_for_new_skus(args.store, uniq)
        print(f"[{args.store}] 待补(不在映射表) 之外结果:")
        print(f"  补齐 added   = {res.get('added')}")
        print(f"  跳过 skipped = {res.get('skipped')}  原因={res.get('reasons')}")
        if res.get("note"):
            print(f"  note = {res['note']}")
        for s in res.get("samples", []):
            print("   ", s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
