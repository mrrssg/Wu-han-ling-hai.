"""Sync Mirakl returns (RT11) into per-store tables for inventory of refunds
and customer return tracking. Mirrors transaction_log_sync_service.py:

- one table per store: {store_key}_returns
- one cursor table: returns_sync_cursor (last_synced_at per store_key)
- incremental via return_last_updated_from + 2h overlap window
- seek pagination via next_page_token
- IP-isolated through _load_network_profile(store_key) - reuses the same
  Brightdata pinned IPs as everywhere else
- one row per return; line items kept as return_lines_json (count+qty
  derived columns for cheap aggregation), full payload in raw_json

RT11 rate limits:
    recommended: every 15 minutes
    maximum:     every 5 minutes (well above what we need)
"""
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from flask import current_app

from app.models.db_manager import DBManager
from app.services.mirakl_shipping_service import (
    _load_network_profile,
    _request_with_retry,
    load_store_config,
)


UTC = timezone.utc


STORE_CONFIGS: Dict[str, Dict[str, str]] = {
    "macy_kuyotq":   {"label": "Macy-Kuyotq",   "table": "macy_kuyotq_returns"},
    "macy_wopet":    {"label": "Macy-Wopet",    "table": "macy_wopet_returns"},
    "lowes_autool":  {"label": "Lowes-Autool",  "table": "lowes_autool_returns"},
    "lowes_yasonic": {"label": "Lowes-Yasonic", "table": "lowes_yasonic_returns"},
}


# RT11 has no per-page size param; Mirakl default seek-pagination page size is
# 100. With 10 pages/run we can absorb ~1000 returns per cron tick, far more
# than realistic daily volume.
MAX_PAGES_PER_RUN = 10
REQUEST_INTERVAL = 5         # seconds between calls
OVERLAP_HOURS = 2            # incremental overlap to catch late-updated rows
DEFAULT_BACKFILL_DAYS = 180  # if no cursor and no --full-from, look back 6 months


INSERT_COLUMNS = [
    "return_id", "rma", "state", "method_code", "reason_code", "rejection_reason_code",
    "order_commercial_id", "order_id", "date_created", "last_updated",
    "description", "label_url",
    "return_address_city", "return_address_state", "return_address_country", "return_address_zip",
    "return_address_street1", "return_address_street2",
    "tracking_carrier_name", "tracking_carrier_code", "tracking_number", "tracking_url",
    "return_lines_count", "return_lines_total_qty", "return_lines_json", "raw_json",
]


def _iso_utc(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso_to_db(s) -> Optional[str]:
    if not s:
        return None
    try:
        text = str(s).replace("Z", "+00:00")
        return datetime.fromisoformat(text).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _ensure_schema(table: str):
    """Create the per-store returns table + the shared cursor table if absent."""
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS order_system.`{table}` (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    return_id CHAR(36) NOT NULL,
                    rma VARCHAR(64),
                    state VARCHAR(32),
                    method_code VARCHAR(48),
                    reason_code VARCHAR(64),
                    rejection_reason_code VARCHAR(48),
                    order_commercial_id VARCHAR(64),
                    order_id VARCHAR(64),
                    date_created DATETIME,
                    last_updated DATETIME,
                    description TEXT,
                    label_url VARCHAR(500),
                    return_address_city VARCHAR(100),
                    return_address_state VARCHAR(50),
                    return_address_country VARCHAR(8),
                    return_address_zip VARCHAR(32),
                    return_address_street1 VARCHAR(255),
                    return_address_street2 VARCHAR(255),
                    tracking_carrier_name VARCHAR(64),
                    tracking_carrier_code VARCHAR(64),
                    tracking_number VARCHAR(128),
                    tracking_url VARCHAR(500),
                    return_lines_count INT,
                    return_lines_total_qty INT,
                    return_lines_json LONGTEXT,
                    raw_json LONGTEXT,
                    created_at_db DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at_db DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_return_id (return_id),
                    INDEX idx_state (state),
                    INDEX idx_order_commercial_id (order_commercial_id),
                    INDEX idx_last_updated (last_updated),
                    INDEX idx_reason_code (reason_code)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS order_system.returns_sync_cursor (
                    store_key VARCHAR(64) PRIMARY KEY,
                    last_synced_at VARCHAR(64) NULL,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
        conn.commit()
    finally:
        conn.close()


def _load_cursor(store_key: str) -> Optional[str]:
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT last_synced_at FROM order_system.returns_sync_cursor WHERE store_key=%s",
                (store_key,),
            )
            row = cursor.fetchone()
            return row["last_synced_at"] if row and row.get("last_synced_at") else None
    finally:
        conn.close()


def _save_cursor(store_key: str, last_synced_at: str):
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO order_system.returns_sync_cursor (store_key, last_synced_at)
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE last_synced_at = VALUES(last_synced_at)
            """, (store_key, last_synced_at))
        conn.commit()
    finally:
        conn.close()


def _api_to_db_row(item: Dict[str, Any]) -> Dict[str, Any]:
    addr = item.get("return_address") or {}
    tracking = item.get("tracking") or {}
    lines = item.get("return_lines") or []
    return {
        "return_id": item.get("id"),
        "rma": item.get("rma"),
        "state": item.get("state"),
        "method_code": item.get("method_code"),
        "reason_code": item.get("reason_code"),
        "rejection_reason_code": item.get("rejection_reason_code"),
        "order_commercial_id": item.get("order_commercial_id"),
        "order_id": item.get("order_id"),
        "date_created": _parse_iso_to_db(item.get("date_created")),
        "last_updated": _parse_iso_to_db(item.get("last_updated")),
        "description": item.get("description"),
        "label_url": item.get("label_url"),
        "return_address_city": addr.get("city"),
        "return_address_state": addr.get("state"),
        "return_address_country": addr.get("country_iso_code"),
        "return_address_zip": addr.get("zip_code"),
        "return_address_street1": addr.get("street1"),
        "return_address_street2": addr.get("street2"),
        "tracking_carrier_name": tracking.get("carrier_name"),
        "tracking_carrier_code": tracking.get("carrier_code"),
        "tracking_number": tracking.get("tracking_number"),
        "tracking_url": tracking.get("tracking_url"),
        "return_lines_count": len(lines),
        "return_lines_total_qty": sum(int(line.get("quantity") or 0) for line in lines),
        "return_lines_json": json.dumps(lines, ensure_ascii=False),
        "raw_json": json.dumps(item, ensure_ascii=False),
    }


def _insert_batch(conn, table: str, items: List[Dict[str, Any]]) -> int:
    if not items:
        return 0
    col_expr = ", ".join(f"`{c}`" for c in INSERT_COLUMNS)
    placeholders = ", ".join(["%s"] * len(INSERT_COLUMNS))
    sql = f"INSERT IGNORE INTO order_system.`{table}` ({col_expr}) VALUES ({placeholders})"
    batch = []
    for item in items:
        row = _api_to_db_row(item)
        batch.append(tuple(row.get(c) for c in INSERT_COLUMNS))
    with conn.cursor() as cursor:
        cursor.executemany(sql, batch)
        inserted = cursor.rowcount or 0
    conn.commit()
    return inserted


def run_returns_sync(
    store_key: str,
    max_pages: int = MAX_PAGES_PER_RUN,
    full_from: Optional[str] = None,
) -> Dict[str, Any]:
    """Incremental RT11 sync.

    Args:
        store_key: which store to sync.
        max_pages: cap API calls per run (default 10 ~ 1000 returns).
        full_from: ISO UTC string; if set, ignore cursor and start from this
            time. Used for first-time backfill. Cursor still advances to the
            newest last_updated seen, so subsequent cron runs go incremental.
    """
    if store_key not in STORE_CONFIGS:
        return {"success": False, "msg": f"unsupported store: {store_key}"}

    store_cfg = STORE_CONFIGS[store_key]
    table = store_cfg["table"]
    _ensure_schema(table)

    base_dir = current_app.config.get("BASE_DIR", current_app.root_path)
    api_cfg = load_store_config(base_dir, store_key)
    api_key = str(api_cfg.get("api_key") or "").strip()
    api_url = str(api_cfg.get("api_url") or "").strip()
    if not api_key:
        return {"success": False, "msg": "missing api key"}

    network = _load_network_profile(store_key)
    headers = {
        "Authorization": api_key,
        "Accept": "application/json",
        "User-Agent": network["user_agent"],
        "Connection": "close",
    }

    cursor_val = _load_cursor(store_key)
    if full_from:
        start_from = full_from
    elif cursor_val:
        try:
            dt = datetime.fromisoformat(cursor_val.replace("Z", "+00:00"))
            start_from = _iso_utc(dt - timedelta(hours=OVERLAP_HOURS))
        except Exception:
            start_from = cursor_val
    else:
        start_from = _iso_utc(datetime.now(UTC) - timedelta(days=DEFAULT_BACKFILL_DAYS))

    print(f"[RETURNS_SYNC][{store_key}] cursor={cursor_val} start_from={start_from} "
          f"max_pages={max_pages} full_from={full_from}")

    pages_fetched = 0
    records_fetched = 0
    records_inserted = 0
    records_duplicate = 0
    latest_updated = cursor_val or ""
    error_msg = ""
    page_token: Optional[str] = None

    conn = DBManager.get_connection()
    try:
        while pages_fetched < max_pages:
            if pages_fetched > 0:
                time.sleep(REQUEST_INTERVAL)

            if page_token:
                params = {"page_token": page_token}
            else:
                params = {"return_last_updated_from": start_from}

            try:
                resp = _request_with_retry(
                    method="GET",
                    url=f"{api_url.rstrip('/')}/api/returns",
                    headers=headers,
                    params=params,
                    proxies=network["proxies"],
                    timeout=60,
                )
            except Exception as exc:
                error_msg = f"request failed: {exc}"
                break

            if resp.status_code != 200:
                error_msg = f"API returned {resp.status_code}: {resp.text[:500]}"
                break

            try:
                payload = resp.json()
            except Exception as exc:
                error_msg = f"invalid json: {exc}"
                break

            data = payload.get("data") or []
            page_token = payload.get("next_page_token")
            pages_fetched += 1
            records_fetched += len(data)

            if data:
                inserted = _insert_batch(conn, table, data)
                records_inserted += inserted
                records_duplicate += len(data) - inserted
                for item in data:
                    lu = item.get("last_updated") or ""
                    if lu > latest_updated:
                        latest_updated = lu

            first_date = data[0].get("last_updated", "")[:10] if data else "-"
            last_date = data[-1].get("last_updated", "")[:10] if data else "-"
            print(f"[RETURNS_SYNC][{store_key}] page={pages_fetched} "
                  f"fetched={len(data)} inserted={records_inserted} "
                  f"dup={records_duplicate} range={first_date}~{last_date}")

            if not page_token or not data:
                break
    except Exception as exc:
        conn.rollback()
        error_msg = str(exc)
    finally:
        conn.close()

    if latest_updated and not error_msg:
        _save_cursor(store_key, latest_updated)

    success = not error_msg
    reached_end = not page_token
    msg = error_msg or (
        "completed - all caught up" if reached_end
        else "completed - more pages available, next run will continue"
    )
    return {
        "success": success,
        "store_key": store_key,
        "msg": msg,
        "reached_end": reached_end,
        "pages_fetched": pages_fetched,
        "records_fetched": records_fetched,
        "records_inserted": records_inserted,
        "records_duplicate": records_duplicate,
        "latest_updated": latest_updated,
        "ip_used": f"{network['proxy_ip']}:{network['proxy_port']}",
    }
