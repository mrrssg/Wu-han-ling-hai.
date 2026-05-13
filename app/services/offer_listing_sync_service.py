"""
Sync Mirakl offer listings via OF52+OF53 into order_system.offerprice_listing.

Differences from the previous version (OF21 paginated GET):
- Uses OF52 async export + OF53 polling -> single bulk download.
- Stores the full raw offer JSON in offerprice_listing.raw_json so OF24
  callers can rebuild a complete update payload without an extra OF22 hop.
- Tracks an incremental cursor (last_request_date) so subsequent runs only
  pull changed offers.

All Mirakl traffic is routed through the store's proxy IP via
`mirakl_offer_api_service` which in turn uses
`mirakl_shipping_service._load_network_profile`.

Only macy_kuyotq is wired up at the moment.
"""
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.models.db_manager import DBManager
from app.services.mirakl_offer_api_service import (
    download_export_chunks,
    poll_offer_export,
    submit_offer_export,
)


STORE_CONFIGS: Dict[str, Dict[str, str]] = {
    "macy_kuyotq": {
        "label": "Macy-Kuyotq",
        "platform": "Macy",
        "shop_name": "kuyotq",
    },
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_cursor_table():
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS order_system.offer_sync_cursor (
                    store_key VARCHAR(64) PRIMARY KEY,
                    last_request_date VARCHAR(64) NULL,
                    last_run_at DATETIME NOT NULL,
                    last_tracking_id VARCHAR(128) NULL,
                    last_offer_count INT NOT NULL DEFAULT 0,
                    last_status VARCHAR(32) NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
        conn.commit()
    finally:
        conn.close()


def _get_cursor(store_key: str) -> Optional[str]:
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT last_request_date FROM order_system.offer_sync_cursor WHERE store_key=%s",
                (store_key,),
            )
            row = cursor.fetchone() or {}
            val = row.get("last_request_date")
            return val if val else None
    finally:
        conn.close()


def _save_cursor(store_key: str, last_request_date: str, tracking_id: str,
                 offer_count: int, status: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """INSERT INTO order_system.offer_sync_cursor
                       (store_key, last_request_date, last_run_at,
                        last_tracking_id, last_offer_count, last_status)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON DUPLICATE KEY UPDATE
                       last_request_date = VALUES(last_request_date),
                       last_run_at = VALUES(last_run_at),
                       last_tracking_id = VALUES(last_tracking_id),
                       last_offer_count = VALUES(last_offer_count),
                       last_status = VALUES(last_status)""",
                (store_key, last_request_date, now, tracking_id, offer_count, status),
            )
        conn.commit()
    finally:
        conn.close()


def _norm(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _to_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _to_bool(v) -> Optional[int]:
    if v is None:
        return None
    return 1 if bool(v) else 0


def _parse_date_only(v) -> Optional[str]:
    if not v:
        return None
    s = str(v)
    return s.split("T")[0][:10]


def _extract_retail(prices: Any) -> Dict[str, Any]:
    """Pick origin_price/discount_price/discount_dates from the first
    retail_prices entry (= default channel).
    """
    out = {
        "origin_price": None,
        "discount_price": None,
        "discount_start": None,
        "discount_end": None,
    }
    if not isinstance(prices, list) or not prices:
        return out
    first = prices[0]
    if not isinstance(first, dict):
        return out
    out["origin_price"] = _to_float(first.get("unit_origin_price"))
    out["discount_price"] = _to_float(first.get("unit_discount_price"))
    out["discount_start"] = _parse_date_only(first.get("discount_start_date"))
    out["discount_end"] = _parse_date_only(first.get("discount_end_date"))
    return out


def _build_row(offer: Dict, store_cfg: Dict[str, str], warehouse_sku: Optional[str],
               source_export_id: str, now_str: str) -> tuple:
    shop_sku = _norm(offer.get("shop_sku"))
    retail = _extract_retail(offer.get("retail_prices"))
    title = _norm(offer.get("product_title"))
    category = _norm(offer.get("category_label") or offer.get("category_code"))
    listed_at = None
    ad = offer.get("active_dates")
    if isinstance(ad, dict):
        listed_at = _parse_date_only(ad.get("started_at"))

    active = offer.get("active")
    if active is None:
        active = (offer.get("state_code") or "").upper() == "ACTIVE"

    status_str = "ACTIVE" if active else "INACTIVE"

    return (
        store_cfg["platform"],
        store_cfg["shop_name"],
        shop_sku,
        warehouse_sku,                     # legacy `sku` column = warehouse_sku
        title,
        category,
        _to_float(offer.get("price")),     # legacy `price` (Dropship = wholesale cost)
        _to_int(offer.get("quantity")),
        status_str,
        listed_at,
        now_str,
        _to_float(offer.get("price")),     # cost_price
        retail["origin_price"],
        retail["discount_price"],
        retail["discount_start"],
        retail["discount_end"],
        _norm(offer.get("state_code")),
        warehouse_sku,
        _to_bool(active),
        json.dumps(offer, ensure_ascii=False),
        source_export_id,
    )


UPSERT_SQL = """
INSERT INTO order_system.offerprice_listing
    (platform, shop_name, shop_sku, sku, title, category, price, quantity, status,
     listed_at, updated_at,
     cost_price, origin_price, discount_price, discount_start_date, discount_end_date,
     state_code, warehouse_sku, active, raw_json, source_export_id)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    sku = VALUES(sku),
    title = VALUES(title),
    category = VALUES(category),
    price = VALUES(price),
    quantity = VALUES(quantity),
    status = VALUES(status),
    listed_at = VALUES(listed_at),
    updated_at = VALUES(updated_at),
    cost_price = VALUES(cost_price),
    origin_price = VALUES(origin_price),
    discount_price = VALUES(discount_price),
    discount_start_date = VALUES(discount_start_date),
    discount_end_date = VALUES(discount_end_date),
    state_code = VALUES(state_code),
    warehouse_sku = VALUES(warehouse_sku),
    active = VALUES(active),
    raw_json = VALUES(raw_json),
    source_export_id = VALUES(source_export_id)
"""


def _fetch_mapping(shop_skus: List[str]) -> Dict[str, str]:
    """Return shop_sku -> warehouse_SKU from autooperate.mapping_table."""
    if not shop_skus:
        return {}
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            chunk = 1000
            out: Dict[str, str] = {}
            for i in range(0, len(shop_skus), chunk):
                part = shop_skus[i:i + chunk]
                placeholders = ",".join(["%s"] * len(part))
                cursor.execute(
                    f"SELECT SKU, warehouse_SKU FROM autooperate.mapping_table "
                    f"WHERE SKU IN ({placeholders})",
                    part,
                )
                for row in cursor.fetchall():
                    sku = str(row.get("SKU") or "").strip()
                    wh = str(row.get("warehouse_SKU") or "").strip()
                    if sku and wh:
                        out[sku] = wh
            return out
    finally:
        conn.close()


def _upsert_offers(offers: List[Dict], store_key: str, source_export_id: str) -> Dict[str, int]:
    store_cfg = STORE_CONFIGS[store_key]
    if not offers:
        return {"total": 0, "affected": 0, "with_warehouse_sku": 0}

    shop_skus = [
        str(o.get("shop_sku") or "").strip()
        for o in offers
        if str(o.get("shop_sku") or "").strip()
    ]
    mapping = _fetch_mapping(shop_skus)

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    with_wh = 0
    for offer in offers:
        shop_sku = str(offer.get("shop_sku") or "").strip()
        if not shop_sku:
            continue
        warehouse_sku = mapping.get(shop_sku)
        if warehouse_sku:
            with_wh += 1
        rows.append(_build_row(offer, store_cfg, warehouse_sku, source_export_id, now_str))

    conn = DBManager.get_connection()
    affected = 0
    try:
        with conn.cursor() as cursor:
            chunk = 500
            for i in range(0, len(rows), chunk):
                cursor.executemany(UPSERT_SQL, rows[i:i + chunk])
                affected += cursor.rowcount or 0
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {"total": len(rows), "affected": affected, "with_warehouse_sku": with_wh}


def run_offer_listing_sync(
    store_key: str,
    *,
    force_full: bool = False,
    include_inactive_on_full: bool = True,
) -> Dict[str, Any]:
    """Top-level entry: OF52 submit -> OF53 poll -> download -> upsert.

    Args:
        store_key: macy_kuyotq for now.
        force_full: ignore cursor and do a full export (use after schema
            changes or as the bootstrap run).
        include_inactive_on_full: when running full, also include inactive
            offers so the DB stays comprehensive. (No effect on differential
            runs - Mirakl always returns both active and inactive in diff
            mode per OF52 docs.)
    """
    if store_key not in STORE_CONFIGS:
        return {"success": False, "msg": f"unsupported store: {store_key}"}

    started = datetime.now()
    _ensure_cursor_table()

    cursor_val = None if force_full else _get_cursor(store_key)
    mode = "full" if not cursor_val else "differential"

    submit = submit_offer_export(
        store_key,
        last_request_date=cursor_val,
        include_inactive=include_inactive_on_full and (mode == "full"),
    )
    tracking_id = submit["tracking_id"]

    # New cursor = the moment we submitted (so the next run starts here).
    new_cursor_value = _utc_now_iso()

    poll = poll_offer_export(store_key, tracking_id)
    urls = poll.get("urls") or []
    if not urls:
        _save_cursor(store_key, cursor_val or "", tracking_id, 0, "completed_no_data")
        return {
            "success": True,
            "store_key": store_key,
            "mode": mode,
            "tracking_id": tracking_id,
            "polls_performed": poll.get("polls_performed"),
            "chunks": 0,
            "offers_fetched": 0,
            "rows_upserted": 0,
            "with_warehouse_sku": 0,
            "duration_seconds": round((datetime.now() - started).total_seconds(), 2),
            "cursor_advanced_to": cursor_val or "(unchanged)",
        }

    offers = download_export_chunks(urls, store_key)
    upsert_stats = _upsert_offers(offers, store_key, source_export_id=tracking_id)

    _save_cursor(store_key, new_cursor_value, tracking_id, len(offers), "completed")

    return {
        "success": True,
        "store_key": store_key,
        "mode": mode,
        "tracking_id": tracking_id,
        "polls_performed": poll.get("polls_performed"),
        "chunks": len(urls),
        "offers_fetched": len(offers),
        "rows_upserted": upsert_stats["affected"],
        "with_warehouse_sku": upsert_stats["with_warehouse_sku"],
        "ip_used": poll.get("ip_used"),
        "duration_seconds": round((datetime.now() - started).total_seconds(), 2),
        "cursor_was": cursor_val or "(none, full mode)",
        "cursor_advanced_to": new_cursor_value,
    }
