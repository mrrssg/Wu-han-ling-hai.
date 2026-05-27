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
from app.services.repricing_stores import REPRICING_STORES, is_supported

# Backward-compat alias - STORE_CONFIGS now derived from the central config.
STORE_CONFIGS: Dict[str, Dict[str, str]] = {
    k: {"label": v["label"], "platform": v["platform"], "shop_name": v["shop_name"]}
    for k, v in REPRICING_STORES.items()
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
            # idempotent column add (table may pre-date this column)
            cursor.execute(
                """SELECT 1 FROM information_schema.columns
                   WHERE table_schema='order_system'
                     AND table_name='offer_sync_cursor'
                     AND column_name='last_new_count' LIMIT 1"""
            )
            if not cursor.fetchone():
                cursor.execute(
                    "ALTER TABLE order_system.offer_sync_cursor "
                    "ADD COLUMN last_new_count INT NOT NULL DEFAULT 0"
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
                 offer_count: int, status: str, new_count: int = 0):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """INSERT INTO order_system.offer_sync_cursor
                       (store_key, last_request_date, last_run_at,
                        last_tracking_id, last_offer_count, last_status, last_new_count)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON DUPLICATE KEY UPDATE
                       last_request_date = VALUES(last_request_date),
                       last_run_at = VALUES(last_run_at),
                       last_tracking_id = VALUES(last_tracking_id),
                       last_offer_count = VALUES(last_offer_count),
                       last_status = VALUES(last_status),
                       last_new_count = VALUES(last_new_count)""",
                (store_key, last_request_date, now, tracking_id, offer_count,
                 status, new_count),
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


def _extract_pricing(offer: Dict) -> Dict[str, Any]:
    """Pick origin_price/discount_price/discount_dates out of one Mirakl offer
    record. The shape differs by mode:

    - Non-Dropship marketplace (e.g. Macy-kuyotq): the customer-facing price
      lives at `prices[0].origin_price` and any volume tiering lives in
      `prices[0].volume_prices`. There is no retail_prices block.
    - Dropship marketplace: `retail_prices[0].unit_origin_price` is the
      customer-facing selling price; `prices` holds the wholesale cost. We
      prefer retail_prices when present.

    Discount fields are read in the same priority order.
    """
    out = {
        "origin_price": None,
        "discount_price": None,
        "discount_start": None,
        "discount_end": None,
    }

    prices = offer.get("prices")
    if isinstance(prices, list) and prices and isinstance(prices[0], dict):
        p0 = prices[0]
        out["origin_price"] = _to_float(p0.get("origin_price"))
        out["discount_price"] = _to_float(p0.get("unit_discount_price"))
        out["discount_start"] = _parse_date_only(p0.get("discount_start_date"))
        out["discount_end"] = _parse_date_only(p0.get("discount_end_date"))

    retail = offer.get("retail_prices")
    if isinstance(retail, list) and retail and isinstance(retail[0], dict):
        r0 = retail[0]
        out["origin_price"] = _to_float(r0.get("unit_origin_price")) or out["origin_price"]
        out["discount_price"] = _to_float(r0.get("unit_discount_price")) or out["discount_price"]
        out["discount_start"] = _parse_date_only(r0.get("discount_start_date")) or out["discount_start"]
        out["discount_end"] = _parse_date_only(r0.get("discount_end_date")) or out["discount_end"]

    return out


def _build_row(offer: Dict, store_cfg: Dict[str, str], warehouse_sku: Optional[str],
               source_export_id: str, now_str: str) -> tuple:
    shop_sku = _norm(offer.get("shop_sku"))
    retail = _extract_pricing(offer)
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
    -- COALESCE keeps a previously-backfilled category alive when the OF52 row
    -- comes in with NULL (OF52 never returns category_label/code; only OF21
    -- does). Without COALESCE every nightly cron would clobber backfilled values.
    category = COALESCE(VALUES(category), category),
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


def _upsert_offers(offers: List[Dict], store_key: str, source_export_id: str) -> Dict[str, Any]:
    store_cfg = STORE_CONFIGS[store_key]
    if not offers:
        return {"total": 0, "affected": 0, "with_warehouse_sku": 0, "new_count": 0, "new_skus": []}

    shop_skus = [
        str(o.get("shop_sku") or "").strip()
        for o in offers
        if str(o.get("shop_sku") or "").strip()
    ]
    mapping = _fetch_mapping(shop_skus)

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    incoming_skus: List[str] = []
    with_wh = 0
    for offer in offers:
        shop_sku = str(offer.get("shop_sku") or "").strip()
        if not shop_sku:
            continue
        warehouse_sku = mapping.get(shop_sku)
        if warehouse_sku:
            with_wh += 1
        incoming_skus.append(shop_sku)
        rows.append(_build_row(offer, store_cfg, warehouse_sku, source_export_id, now_str))

    conn = DBManager.get_connection()
    affected = 0
    new_skus: List[str] = []
    try:
        with conn.cursor() as cursor:
            # Diff incoming SKUs against current DB so we can return the exact
            # set that will be INSERTed (vs UPDATEd). Needed for the OF21
            # category-backfill step in run_offer_listing_sync; rowcount alone
            # cannot tell INSERTs apart from UPDATEs in an ON DUPLICATE batch.
            existing_skus: set = set()
            chunk_sku = 1000
            for i in range(0, len(incoming_skus), chunk_sku):
                part = incoming_skus[i:i + chunk_sku]
                placeholders = ",".join(["%s"] * len(part))
                cursor.execute(
                    f"""SELECT shop_sku FROM order_system.offerprice_listing
                         WHERE platform=%s AND shop_name=%s
                           AND shop_sku IN ({placeholders})""",
                    (store_cfg["platform"], store_cfg["shop_name"], *part),
                )
                existing_skus.update(str(r.get("shop_sku") or "").strip()
                                     for r in cursor.fetchall())
            new_skus = [s for s in incoming_skus if s not in existing_skus]

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

    return {
        "total": len(rows),
        "affected": affected,
        "with_warehouse_sku": with_wh,
        "new_count": len(new_skus),
        "new_skus": new_skus,
    }


# =============================================================================
# Category backfill via OF21 (one OF21 call per SKU)
# OF52 export does not return category_label/code; OF21 by sku does.
# Used both at end of every sync (for newly inserted SKUs) and by the
# one-shot scripts/backfill_categories.py.
# =============================================================================

CATEGORY_BACKFILL_SLEEP_SECONDS = 2.0   # spacing between OF21 calls; Mirakl has
                                        # no hard cap but ~2s is the observed
                                        # natural response time, so this both
                                        # paces us and avoids stacking requests.


def _update_category_row(store_cfg: Dict[str, str], shop_sku: str, category: Optional[str]) -> int:
    if not category:
        return 0
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """UPDATE order_system.offerprice_listing
                      SET category=%s
                    WHERE platform=%s AND shop_name=%s AND shop_sku=%s""",
                (category, store_cfg["platform"], store_cfg["shop_name"], shop_sku),
            )
            affected = cursor.rowcount or 0
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return affected


def backfill_categories_via_of21(store_key: str, shop_skus: List[str],
                                 *, sleep_seconds: float = CATEGORY_BACKFILL_SLEEP_SECONDS,
                                 progress_every: int = 100) -> Dict[str, Any]:
    """Call OF21 for each shop_sku and write category_label into
    offerprice_listing.category. Single-SKU failures are logged and skipped.

    Returns:
        {attempted, updated, errors, missing_in_mirakl, no_category}
    """
    import time
    from app.services.mirakl_offer_api_service import get_offer_by_sku

    store_cfg = STORE_CONFIGS[store_key]
    attempted = 0
    updated = 0
    errors = 0
    missing = 0
    no_cat = 0
    started_at = datetime.now()

    for idx, sku in enumerate(shop_skus, 1):
        attempted += 1
        try:
            offer = get_offer_by_sku(store_key, sku)
            cat = (offer.get("category_label") or offer.get("category_code") or "").strip()
            if not cat:
                no_cat += 1
            else:
                if _update_category_row(store_cfg, sku, cat) > 0:
                    updated += 1
        except RuntimeError as exc:
            # SKU is gone from Mirakl, or returned no offer
            msg = str(exc)
            if "no offer" in msg:
                missing += 1
            else:
                errors += 1
            print(f"[backfill][{store_key}] sku={sku} error: {msg[:200]}")
        except Exception as exc:
            errors += 1
            print(f"[backfill][{store_key}] sku={sku} unexpected: {exc}")

        if idx % progress_every == 0:
            elapsed = (datetime.now() - started_at).total_seconds()
            rate = idx / elapsed if elapsed > 0 else 0
            remain_sec = (len(shop_skus) - idx) / rate if rate > 0 else 0
            print(f"[backfill][{store_key}] {idx}/{len(shop_skus)}  "
                  f"updated={updated} missing={missing} no_cat={no_cat} errors={errors}  "
                  f"eta={int(remain_sec//60)}m{int(remain_sec%60)}s")

        if sleep_seconds > 0 and idx < len(shop_skus):
            time.sleep(sleep_seconds)

    return {
        "attempted": attempted,
        "updated": updated,
        "errors": errors,
        "missing_in_mirakl": missing,
        "no_category": no_cat,
        "duration_seconds": round((datetime.now() - started_at).total_seconds(), 1),
    }


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

    _save_cursor(store_key, new_cursor_value, tracking_id, len(offers),
                 "completed", new_count=upsert_stats["new_count"])

    # OF52 never returns category, so newly inserted SKUs land with NULL.
    # Catch them now via OF21 (one call per SKU, ~2s each, no Mirakl rate cap).
    # Steady-state this is a handful of calls/day per store.
    category_backfill = None
    new_skus = upsert_stats.get("new_skus") or []
    if new_skus:
        print(f"[OF52][{store_key}] backfilling category for {len(new_skus)} new SKUs via OF21 ...")
        try:
            category_backfill = backfill_categories_via_of21(store_key, new_skus)
        except Exception as exc:
            # never fail the OF52 sync just because OF21 backfill blew up;
            # the one-shot scripts/backfill_categories.py can pick up NULLs later.
            print(f"[OF52][{store_key}] category backfill aborted: {exc}")
            category_backfill = {"error": str(exc)[:200]}

    return {
        "success": True,
        "store_key": store_key,
        "mode": mode,
        "tracking_id": tracking_id,
        "polls_performed": poll.get("polls_performed"),
        "chunks": len(urls),
        "offers_fetched": len(offers),
        "rows_upserted": upsert_stats["affected"],
        "new_offers": upsert_stats["new_count"],
        "with_warehouse_sku": upsert_stats["with_warehouse_sku"],
        "ip_used": poll.get("ip_used"),
        "duration_seconds": round((datetime.now() - started).total_seconds(), 2),
        "cursor_was": cursor_val or "(none, full mode)",
        "cursor_advanced_to": new_cursor_value,
        "category_backfill": category_backfill,
    }
