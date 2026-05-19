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
#
# IMPORTANT: we resolve record_id LIVE from Feishu (not from our DB cache).
# Reason: 2026-05-19 incident - the Feishu table was rebuilt by operations
# (record_ids changed), our DB cache held stale ids, and every batch_update
# silently failed with code 1254043 RecordIdNotFound. The print-and-continue
# error handler hid this from the operator.
# =============================================================================

def _build_live_sku_to_rid_map(store_key: str) -> Dict[str, str]:
    """Hit Feishu now (full table scan) and return the latest
    warehouse_sku -> record_id mapping. ~20s for a 7000-row table.
    """
    items = fetch_all_records(store_key)
    out: Dict[str, str] = {}
    for it in items:
        f = it.get("fields", {})
        sku_raw = _unwrap_text(f.get("供应商SKU"))
        if not sku_raw:
            continue
        sku = str(sku_raw).strip()
        rid = it.get("record_id")
        if sku and rid:
            out[sku] = rid
    return out


def _persist_record_ids(pairs: List[Tuple[str, str]], store_key: str):
    """Best-effort: refresh offer_pricing_config.feishu_record_id with the
    record_ids we just resolved live. Idempotent. Never raises.
    """
    if not pairs:
        return
    try:
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.executemany(
                    """UPDATE order_system.offer_pricing_config
                          SET feishu_record_id = %s
                        WHERE store_key = %s AND warehouse_sku = %s""",
                    [(rid, store_key, wh) for (wh, rid) in pairs],
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        print(f"[feishu_writeback] DB rid persist failed (non-fatal): {exc}")


def write_supplier_prices_to_feishu(
    updates: List[Dict[str, Any]],
    store_key: str = "macy_kuyotq",
) -> Dict[str, Any]:
    """`updates`: list of {"warehouse_sku": str, "supplier_price": float}.

    Writes each value into the Feishu Mirakl table's `供应商价格` field.
    record_id is resolved LIVE from Feishu each call (not from DB cache),
    so the writeback survives Feishu-side table rebuilds.

    Returns dict with:
      - sent           : int  (records flushed via batch_update successfully)
      - not_found      : [warehouse_sku, ...] (SKU exists in our DB but Feishu
                          has no record with that SKU - real data gap)
      - failed_chunks  : [str, ...] (batch_update calls Feishu accepted the
                          HTTP but returned non-zero code; surface so caller
                          can alert)
      - error          : str (only set on a hard exception)

    Failure-tolerant: never raises - upstream price push is what matters.
    But unlike before, problems are SURFACED in the return value instead
    of being silently print-and-swallowed.
    """
    if not updates or not _store_supported(store_key):
        return {"sent": 0, "not_found": []}

    src = FEISHU_SOURCES[store_key]
    app_token = src["app_token"]
    table_id = src["table_id"]

    # Pull the latest sku->record_id from Feishu directly.
    try:
        record_map = _build_live_sku_to_rid_map(store_key)
    except Exception as exc:
        print(f"[feishu_writeback] fetch_all_records failed: {exc}")
        return {
            "sent": 0,
            "not_found": [u.get("warehouse_sku") for u in updates if u.get("warehouse_sku")],
            "error": f"fetch_all_records failed: {exc}",
        }

    payload_records: List[Dict[str, Any]] = []
    rids_to_persist: List[Tuple[str, str]] = []
    not_found: List[str] = []
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
        rids_to_persist.append((wh, rid))

    if not payload_records:
        return {"sent": 0, "not_found": not_found}

    sent_total = 0
    failed_chunks: List[str] = []
    try:
        token = _get_tenant_token()
        H = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url = (
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/"
            f"{app_token}/tables/{table_id}/records/batch_update"
        )
        chunk = 500
        for i in range(0, len(payload_records), chunk):
            body = {"records": payload_records[i:i + chunk]}
            r = requests.post(url, headers=H, json=body, timeout=60)
            try:
                data = r.json() if r.content else {}
            except Exception:
                data = {"_non_json": r.text[:500]}
            if data.get("code") != 0:
                msg = (f"chunk {i // chunk + 1}: http={r.status_code} "
                       f"resp={data}")
                print(f"[feishu_writeback] {msg}")
                failed_chunks.append(msg)
                continue
            sent_total += len(body["records"])
            time.sleep(0.2)
    except Exception as exc:
        print(f"[feishu_writeback] exception: {exc}")
        return {
            "sent": sent_total,
            "not_found": not_found,
            "failed_chunks": failed_chunks,
            "error": str(exc),
        }

    # Persist the live record_ids back to DB so future cache lookups (and
    # any other consumer reading offer_pricing_config.feishu_record_id) see
    # the current values.
    if sent_total > 0:
        _persist_record_ids(rids_to_persist, store_key)

    result: Dict[str, Any] = {"sent": sent_total, "not_found": not_found}
    if failed_chunks:
        result["failed_chunks"] = failed_chunks
    return result
