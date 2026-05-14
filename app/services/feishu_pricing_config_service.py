"""
Sync the per-SKU pricing configuration from Feishu bitable into
order_system.offer_pricing_config.

Feishu source (Macy-kuyotq):
    app_token  QEeubiXYGa83zXs3Zt8cSSJPnih
    table_id   tblfyStm2eu3hp1Q  ("Macy-kuyotq-Mirakl")

Fields synced (all are inputs to the Feishu Formula chain):
    供应商SKU         -> warehouse_sku    (PK)
    供应商           -> supplier         ('Costway' | 'Vevor' | ...)
    活动折扣         -> discount_factor
    佣金比例         -> commission_rate
    退货运费(基础)    -> return_shipping_base
    长in / 宽in / 高in / 重LB -> length_in / width_in / height_in / weight_lb
    record_id        -> feishu_record_id

This module does NOT call any Mirakl API. It only talks to Feishu + autoweb DB.
"""
import os
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from flask import current_app

from app.models.db_manager import DBManager


FEISHU_APP_ID = "cli_a940a2a1067adbd2"
FEISHU_APP_SECRET = "i2mKLGVzUDmu4v0U9HYEYdMGc0ZvZAgU"

# Per-store Feishu source - derived from the central repricing_stores config.
from app.services.repricing_stores import REPRICING_STORES, is_supported as _store_supported

FEISHU_SOURCES: Dict[str, Dict[str, str]] = {
    k: {
        "app_token": v["feishu_app_token"],
        "table_id": v["feishu_table_id"],
        "label": v["feishu_label"],
    }
    for k, v in REPRICING_STORES.items()
}

PAGE_SIZE = 500
REQUEST_TIMEOUT = 30
PAGE_DELAY_SECONDS = 0.4


def _get_tenant_token() -> str:
    r = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"feishu auth failed: {data}")
    return data["tenant_access_token"]


def _unwrap_text(v):
    """Feishu Text fields return [{'text': '...', 'type': 'text'}, ...]."""
    if isinstance(v, list) and v and isinstance(v[0], dict) and "text" in v[0]:
        # join multi-segment text
        return "".join(seg.get("text") or "" for seg in v if isinstance(seg, dict))
    return v


def _to_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_all_records(store_key: str) -> List[Dict]:
    """Page through the entire Feishu table for one store. Returns the raw
    items (record dicts) with our needed fields only.
    """
    src = FEISHU_SOURCES.get(store_key)
    if not src:
        raise ValueError(f"unsupported store_key: {store_key}")

    token = _get_tenant_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    base_url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/"
        f"{src['app_token']}/tables/{src['table_id']}/records/search"
    )
    body = {
        "field_names": [
            "供应商SKU",
            "供应商",
            "活动折扣",
            "佣金比例",
            "退货运费(基础)",
            "长in", "宽in", "高in", "重LB",
        ]
    }

    all_items: List[Dict] = []
    page_token: Optional[str] = None
    page_num = 0
    while True:
        page_num += 1
        if page_num > 1:
            time.sleep(PAGE_DELAY_SECONDS)
        url = f"{base_url}?page_size={PAGE_SIZE}"
        if page_token:
            url += f"&page_token={page_token}"
        r = requests.post(url, headers=headers, json=body, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        payload = r.json()
        if payload.get("code") != 0:
            raise RuntimeError(f"feishu search failed: {payload}")
        data = payload.get("data") or {}
        items = data.get("items") or []
        all_items.extend(items)
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
        if not page_token:
            break
    return all_items


def transform_record(rec: Dict, store_key: str) -> Optional[Tuple]:
    """Pull and validate one Feishu record into a DB row tuple, or return None
    if it should be skipped (no warehouse_sku).

    Returns:
        (warehouse_sku, store_key, supplier, discount_factor, commission_rate,
         return_shipping_base, length_in, width_in, height_in, weight_lb,
         feishu_record_id, now)
    """
    f = rec.get("fields", {})
    record_id = rec.get("record_id")

    warehouse_sku_raw = _unwrap_text(f.get("供应商SKU"))
    if not warehouse_sku_raw:
        return None
    warehouse_sku = str(warehouse_sku_raw).strip()
    if not warehouse_sku:
        return None

    supplier = f.get("供应商")
    if isinstance(supplier, dict):
        supplier = supplier.get("text")
    supplier = (supplier or "").strip() or None

    discount_factor = _to_float(f.get("活动折扣"))
    commission_rate = _to_float(f.get("佣金比例"))
    return_base = _to_float(f.get("退货运费(基础)"))
    length_in = _to_float(f.get("长in"))
    width_in = _to_float(f.get("宽in"))
    height_in = _to_float(f.get("高in"))
    weight_lb = _to_float(f.get("重LB"))

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return (
        warehouse_sku,
        store_key,
        supplier,
        discount_factor,
        commission_rate,
        return_base,
        length_in,
        width_in,
        height_in,
        weight_lb,
        record_id,
        now,
    )


def upsert_batch(rows: Iterable[Tuple]) -> Tuple[int, int]:
    """Bulk upsert into order_system.offer_pricing_config. Returns
    (rows_seen, rows_affected_estimate).
    """
    rows = list(rows)
    if not rows:
        return 0, 0

    sql = """
        INSERT INTO order_system.offer_pricing_config
            (warehouse_sku, store_key, supplier, discount_factor, commission_rate,
             return_shipping_base, length_in, width_in, height_in, weight_lb,
             feishu_record_id, last_synced_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            supplier             = VALUES(supplier),
            discount_factor      = VALUES(discount_factor),
            commission_rate      = VALUES(commission_rate),
            return_shipping_base = VALUES(return_shipping_base),
            length_in            = VALUES(length_in),
            width_in             = VALUES(width_in),
            height_in            = VALUES(height_in),
            weight_lb            = VALUES(weight_lb),
            feishu_record_id     = VALUES(feishu_record_id),
            last_synced_at       = VALUES(last_synced_at)
    """

    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            # Chunk into 500/batch to keep packets small.
            chunk = 500
            affected = 0
            for i in range(0, len(rows), chunk):
                cursor.executemany(sql, rows[i:i + chunk])
                affected += cursor.rowcount or 0
        conn.commit()
        return len(rows), affected
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def run_sync(store_key: str = "macy_kuyotq") -> Dict:
    """Pull all Feishu records and upsert into offer_pricing_config. Returns
    a summary dict for the cron output.
    """
    started = datetime.now()
    items = fetch_all_records(store_key)
    seen = len(items)

    rows = []
    skipped_no_sku = 0
    duplicate_skus = 0
    seen_skus: set = set()
    for rec in items:
        row = transform_record(rec, store_key)
        if row is None:
            skipped_no_sku += 1
            continue
        if row[0] in seen_skus:
            duplicate_skus += 1
            continue
        seen_skus.add(row[0])
        rows.append(row)

    total, affected = upsert_batch(rows)

    duration = (datetime.now() - started).total_seconds()
    return {
        "store_key": store_key,
        "feishu_records_fetched": seen,
        "valid_rows": total,
        "skipped_no_sku": skipped_no_sku,
        "duplicate_skus": duplicate_skus,
        "db_affected_rows": affected,
        "duration_seconds": round(duration, 2),
        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def freshness_status(store_key: str = "macy_kuyotq") -> Dict:
    """Quick helper for health-check pages. Returns the MAX(last_synced_at)
    and number of rows for a store.
    """
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """SELECT COUNT(*) AS c, MAX(last_synced_at) AS latest
                   FROM order_system.offer_pricing_config WHERE store_key=%s""",
                (store_key,),
            )
            row = cursor.fetchone() or {}
            return {
                "store_key": store_key,
                "rows": int(row.get("c") or 0),
                "latest_sync": str(row.get("latest") or ""),
            }
    finally:
        conn.close()


# =============================================================================
# Push the latest supplier_price back into the Feishu Mirakl table's
# `供应商价格` field. Called after a successful OF24 push so the Feishu
# `成本/利润` Formula columns stay in sync with what Mirakl actually sees.
# =============================================================================

def _get_feishu_record_ids(warehouse_skus: List[str], store_key: str) -> Dict[str, str]:
    """warehouse_sku -> feishu_record_id from our DB cache (no Feishu lookup)."""
    if not warehouse_skus:
        return {}
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            chunk = 1000
            out: Dict[str, str] = {}
            for i in range(0, len(warehouse_skus), chunk):
                part = warehouse_skus[i:i + chunk]
                placeholders = ",".join(["%s"] * len(part))
                cursor.execute(
                    f"SELECT warehouse_sku, feishu_record_id "
                    f"FROM order_system.offer_pricing_config "
                    f"WHERE store_key=%s AND warehouse_sku IN ({placeholders})",
                    [store_key] + part,
                )
                for r in cursor.fetchall():
                    rid = r.get("feishu_record_id")
                    if rid:
                        out[r["warehouse_sku"]] = rid
            return out
    finally:
        conn.close()


def write_supplier_prices_to_feishu(
    updates: List[Dict[str, Any]],
    store_key: str = "macy_kuyotq",
) -> Dict[str, Any]:
    """`updates`: list of {"warehouse_sku": str, "supplier_price": float}.

    Writes each value into the Feishu Mirakl table's `供应商价格` field by
    record_id (cached in our DB at offer_pricing_config.feishu_record_id).

    Returns:
        {"sent": int, "not_found": [warehouse_sku, ...]}

    Failure-tolerant: never raises - the upstream push is more important.
    """
    if not updates or not _store_supported(store_key):
        return {"sent": 0, "not_found": []}

    src = FEISHU_SOURCES[store_key]
    app_token = src["app_token"]
    table_id = src["table_id"]

    skus = [u.get("warehouse_sku") for u in updates if u.get("warehouse_sku")]
    record_map = _get_feishu_record_ids(skus, store_key)

    payload_records = []
    not_found = []
    for u in updates:
        wh = u.get("warehouse_sku")
        sp = u.get("supplier_price")
        if not wh or sp is None:
            continue
        rid = record_map.get(wh)
        if not rid:
            not_found.append(wh)
            continue
        try:
            sp_f = round(float(sp), 4)
        except (TypeError, ValueError):
            continue
        payload_records.append({
            "record_id": rid,
            "fields": {"供应商价格": sp_f},
        })

    if not payload_records:
        return {"sent": 0, "not_found": not_found}

    try:
        token = _get_tenant_token()
        H = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url = (
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/"
            f"{app_token}/tables/{table_id}/records/batch_update"
        )
        sent_total = 0
        chunk = 500
        for i in range(0, len(payload_records), chunk):
            body = {"records": payload_records[i:i + chunk]}
            r = requests.post(url, headers=H, json=body, timeout=60)
            data = r.json() if r.content else {}
            if data.get("code") != 0:
                # log and continue with next chunk - don't break the world
                print(f"[feishu_writeback] chunk failed: {data}")
                continue
            sent_total += len(body["records"])
            time.sleep(0.2)
    except Exception as exc:
        print(f"[feishu_writeback] exception: {exc}")
        return {"sent": 0, "not_found": not_found, "error": str(exc)}

    return {"sent": sent_total, "not_found": not_found}
