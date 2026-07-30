# -*- coding: utf-8 -*-
"""从飞书 *-Mirakl 表全量对账补 SKU 映射(绕开 offer 同步)。

用途:当 offer 同步落后/失败,但店铺已"通过"很多新 listing 时,直接从该店铺
飞书表**一遍分页扫描**成对拿 (Shop SKU, 供应商SKU),把不在 autooperate.mapping_table
的补进去。零逐个 API 调用——供应商存在性/冲突/运营全在本地库判。

五道校验(沿用 mapping_backfill_service 口径):
  1. 绝不跨店:只读该店铺自己的飞书表
  2. 精确:飞书行里的值直接用,不模糊
  3. 冲突即跳过:同一 shop_sku 在表里出现多个不同供应商SKU → 跳过
  4. 供应商SKU须真实:在豪雅/司顺/大建/致欧价格表里查得到才写
  5. 只补空缺:mapping_table 已有的绝不覆盖

    python scripts/reconcile_mapping_from_feishu.py --store lowes_autool [--dry-run]
"""
import argparse
import json
import sys

import requests

import app as app_module
from app.models.db_manager import DBManager
from app.services.mapping_backfill_service import (
    FEISHU_APP, STORE_FEISHU, SUPPLIER_TABLES, _gt, _operator_of,
)


def _scan_feishu_pairs(store_key: str):
    """一遍分页扫描,返回 shop_sku -> set(供应商SKU) 及命中字段。"""
    from app.services.listing_sentinel_service import _token
    tbl, fields = STORE_FEISHU[store_key]
    headers = {"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"}
    url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP}"
           f"/tables/{tbl}/records/search?page_size=500")
    pairs, page_token, pages = {}, None, 0
    while True:
        u = url + (f"&page_token={page_token}" if page_token else "")
        r = requests.post(u, headers=headers, data=json.dumps({}).encode("utf-8"),
                          timeout=30).json()
        data = r.get("data") or {}
        for it in data.get("items") or []:
            f = it.get("fields", {})
            shop = ""
            for field in fields:
                v = _gt(f.get(field)).strip()
                if v:
                    shop = v
                    break
            if not shop:
                continue
            sup = _gt(f.get("供应商SKU")).strip()
            if sup:
                pairs.setdefault(shop, set()).add(sup)
            else:
                pairs.setdefault(shop, set())
        pages += 1
        page_token = data.get("page_token")
        print(f"  ...第{pages}页,累计 {len(pairs)} 个 shop SKU", flush=True)
        if not data.get("has_more") or not page_token:
            break
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default="lowes_autool", choices=list(STORE_FEISHU))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    app = app_module.create_app()
    with app.app_context():
        print(f"[{args.store}] 扫描飞书表...", flush=True)
        pairs = _scan_feishu_pairs(args.store)
        print(f"[{args.store}] 飞书共 {len(pairs)} 个 shop SKU", flush=True)

        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT SKU FROM autooperate.mapping_table")
                have = {str(r["SKU"]).strip() for r in cur.fetchall()}
                supplier = set()
                for t in SUPPLIER_TABLES:
                    try:
                        cur.execute(f"SELECT SKU FROM {t}")
                        supplier.update(str(r["SKU"]).strip() for r in cur.fetchall() if r["SKU"])
                    except Exception:
                        pass
        finally:
            conn.close()

        reasons, to_insert, samples = {}, [], []

        def skip(sku, why):
            reasons[why] = reasons.get(why, 0) + 1
            if len(samples) < 25:
                samples.append(f"跳过 {sku}: {why}")

        for shop, sups in pairs.items():
            if shop in have:
                continue                       # 闸5 只补空缺
            if len(sups) == 0:
                skip(shop, "飞书无供应商SKU")
                continue
            if len(sups) > 1:
                skip(shop, "飞书供应商SKU冲突")   # 闸3
                continue
            sup = next(iter(sups))
            if sup not in supplier:
                skip(shop, "供应商SKU价格表中不存在")  # 闸4
                continue
            owner = _operator_of(shop)
            if not owner:
                skip(shop, "前缀判不出运营")
                continue
            to_insert.append((shop, sup, owner))
            if len(samples) < 25:
                samples.append(f"补齐 {shop} → {sup} ({owner})")

        print(f"\n[{args.store}] 待补(不在映射表): {len(to_insert)}  跳过: {sum(reasons.values())}")
        print(f"  跳过原因 = {reasons}")
        for s in samples:
            print("   ", s)

        if args.dry_run:
            print("  [dry-run] 不写库")
            return 0

        if to_insert:
            conn = DBManager.get_connection()
            try:
                with conn.cursor() as cur:
                    cur.executemany("""
                        INSERT INTO autooperate.mapping_table (SKU, warehouse_SKU, owner)
                        VALUES (%s,%s,%s)
                        ON DUPLICATE KEY UPDATE SKU=SKU""", to_insert)
                    for shop, sup, _o in to_insert:
                        cur.execute("""
                            UPDATE order_system.offerprice_listing
                            SET warehouse_sku=%s, sku=%s
                            WHERE shop_sku=%s AND (warehouse_sku IS NULL OR warehouse_sku='')""",
                            (sup, sup, shop))
                conn.commit()
            finally:
                conn.close()
            print(f"\n[{args.store}] ✅ 已补 {len(to_insert)} 条映射进 mapping_table")
    return 0


if __name__ == "__main__":
    sys.exit(main())
