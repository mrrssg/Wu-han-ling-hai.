# -*- coding: utf-8 -*-
"""SKU映射自动补齐（2026-07-21用户拍板）。

offer同步后，新offer的shop_sku若不在 autooperate.mapping_table，就从该店铺自己的
飞书 *-Mirakl 表按店铺SKU精确匹配，读「供应商SKU」补进映射表，并回填offer.warehouse_sku。

**千万别对应错——五道校验（任一不过就跳过，绝不硬填）**：
  1. 绝不跨店：每个店铺硬绑自己的飞书表(STORE_FEISHU)，不会拿别店的表
  2. 只认精确匹配：飞书按店铺SKU字段 is 精确查，不模糊
  3. 冲突即跳过：一个shop_sku在飞书匹配到多行且供应商SKU不一致 → 跳过
  4. 供应商SKU必须真实存在：在豪雅/司顺/大建/致欧价格表里查得到才写(挡错字/空值)
  5. 只补空缺：mapping_table已有的绝不覆盖(SKU是主键,预筛+ON DUP不改warehouse)
运营(owner)按shop_sku前缀判(MD=刘梦蝶/MR=明瑞瑞/YC=朱以超)——新品前缀即建listing的
运营,天生正确;历史转手的例外只影响老SKU,不影响新增补齐。
"""
import json
import re
from typing import Any, Dict, List, Optional

import requests

from app.models.db_manager import DBManager

# 店铺 → (飞书表, 店铺SKU候选字段按优先级)。各表SKU字段名不统一(实测:Kuyotq用「店铺SKU」)
STORE_FEISHU = {
    "lowes_autool":  ("tblGp3uvtOe99vjY", ["Shop SKU", "店铺SKU"]),
    "lowes_yasonic": ("tbldeuRJOoJBfX2g", ["Shop SKU", "店铺SKU"]),
    "macy_wopet":    ("tbla2i1OwdwlCweK", ["Shop SKU", "店铺SKU"]),
    "macy_kuyotq":   ("tblfyStm2eu3hp1Q", ["店铺SKU", "Shop SKU"]),
}
FEISHU_APP = "QEeubiXYGa83zXs3Zt8cSSJPnih"

# 供应商价格表(校验供应商SKU真实存在)
SUPPLIER_TABLES = ["autooperate.newestdropship", "autooperate.newestdropship_vevor",
                   "autooperate.newestdropship_dajian", "autooperate.newestdropship_songmics"]

OP_BY_PREFIX = {"MD": "刘梦蝶", "MR": "明瑞瑞", "YC": "朱以超"}
_OP_RE = re.compile(r"(MD|MR|YC)(LW|MC)", re.I)


def _operator_of(shop_sku: str) -> Optional[str]:
    m = _OP_RE.search((shop_sku or "").upper())
    return OP_BY_PREFIX.get(m.group(1).upper()) if m else None


def _gt(v) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, list) and v and isinstance(v[0], dict):
        return "".join(x.get("text", "") for x in v)
    if isinstance(v, dict):
        return v.get("text") or ""
    return str(v) if v is not None else ""


def _feishu_lookup(headers, tbl: str, fields: List[str], shop_sku: str) -> Dict[str, Any]:
    """按候选字段精确查该shop_sku。返回 {status, supplier_sku, note}。
    status: ok / not_found / conflict"""
    url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP}"
           f"/tables/{tbl}/records/search?page_size=10")
    for field in fields:
        body = {"filter": {"conjunction": "and", "conditions": [
            {"field_name": field, "operator": "is", "value": [shop_sku]}]}}
        try:
            r = requests.post(url, headers=headers,
                              data=json.dumps(body).encode("utf-8"), timeout=30).json()
        except Exception as exc:
            return {"status": "error", "note": str(exc)[:120]}
        items = (r.get("data") or {}).get("items") or []
        if not items:
            continue
        # 精确复核 + 供应商SKU一致性(多行冲突则跳过)
        skus = set()
        for it in items:
            f = it["fields"]
            if _gt(f.get(field)).strip() != shop_sku:
                continue
            wh = _gt(f.get("供应商SKU")).strip()
            if wh:
                skus.add(wh)
        if len(skus) == 1:
            return {"status": "ok", "supplier_sku": next(iter(skus)), "field": field}
        if len(skus) > 1:
            return {"status": "conflict", "note": f"{field}匹配到多个供应商SKU:{sorted(skus)}"}
    return {"status": "not_found"}


def backfill_mapping_for_new_skus(store_key: str, shop_skus: List[str]) -> Dict[str, Any]:
    """给一批shop_sku补映射。只处理不在mapping_table里的。返回小结。"""
    cfg = STORE_FEISHU.get(store_key)
    summary = {"store_key": store_key, "input": len(shop_skus or []),
               "added": 0, "skipped": 0, "reasons": {}, "samples": []}
    if not cfg or not shop_skus:
        summary["note"] = "store not configured for mapping backfill" if not cfg else "no skus"
        return summary
    tbl, fields = cfg

    # 只处理不在mapping_table的(千万不覆盖已有)
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            todo = []
            for i in range(0, len(shop_skus), 800):
                part = [s for s in shop_skus[i:i + 800] if s]
                if not part:
                    continue
                ph = ",".join(["%s"] * len(part))
                cur.execute(f"SELECT SKU FROM autooperate.mapping_table WHERE SKU IN ({ph})", part)
                have = {str(r["SKU"]).strip() for r in cur.fetchall()}
                todo += [s for s in part if s not in have]
            # 供应商SKU全集(校验存在性,一次性载入)
            supplier_skus = set()
            for t in SUPPLIER_TABLES:
                try:
                    cur.execute(f"SELECT SKU FROM {t}")
                    supplier_skus.update(str(r["SKU"]).strip() for r in cur.fetchall() if r["SKU"])
                except Exception:
                    pass
    finally:
        conn.close()

    if not todo:
        summary["note"] = "所有SKU都已在映射表"
        return summary

    from app.services.listing_sentinel_service import _token
    headers = {"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"}

    def _skip(sku, reason):
        summary["skipped"] += 1
        summary["reasons"][reason] = summary["reasons"].get(reason, 0) + 1
        if len(summary["samples"]) < 20:
            summary["samples"].append(f"跳过 {sku}: {reason}")

    to_insert = []   # (sku, warehouse_sku, owner)
    for sku in todo:
        res = _feishu_lookup(headers, tbl, fields, sku)
        st = res["status"]
        if st == "not_found":
            _skip(sku, "飞书表无此SKU")
            continue
        if st == "conflict":
            _skip(sku, "飞书供应商SKU冲突")
            continue
        if st == "error":
            _skip(sku, "飞书查询异常")
            continue
        wh = res["supplier_sku"]
        if wh not in supplier_skus:
            _skip(sku, "供应商SKU在价格表中不存在")
            continue
        owner = _operator_of(sku)
        if not owner:
            _skip(sku, "前缀判不出运营")
            continue
        to_insert.append((sku, wh, owner))
        if len(summary["samples"]) < 20:
            summary["samples"].append(f"补齐 {sku} → {wh} ({owner})")

    if to_insert:
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cur:
                # SKU是主键;ON DUP不动warehouse_SKU(并发下也绝不覆盖已有)
                cur.executemany("""
                    INSERT INTO autooperate.mapping_table (SKU, warehouse_SKU, owner)
                    VALUES (%s,%s,%s)
                    ON DUPLICATE KEY UPDATE SKU=SKU""", to_insert)
                # 回填offer的warehouse_sku(legacy sku列同步)
                for sku, wh, _o in to_insert:
                    cur.execute("""
                        UPDATE order_system.offerprice_listing
                        SET warehouse_sku=%s, sku=%s
                        WHERE shop_sku=%s AND (warehouse_sku IS NULL OR warehouse_sku='')""",
                        (wh, wh, sku))
            conn.commit()
        finally:
            conn.close()
        summary["added"] = len(to_insert)

    return summary
