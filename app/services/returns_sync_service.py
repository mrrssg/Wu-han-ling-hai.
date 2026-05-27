"""Sync Mirakl returns (RT11) into a single shared table for inventory of
refunds and customer return tracking.

Design (mirrors offerprice_listing's "one table for all stores" approach):
- one table: mirakl_returns, with platform + shop_name columns
- one cursor table: returns_sync_cursor (last_synced_at per store_key)
- UNIQUE (platform, shop_name, return_id) for cross-store-safe dedup
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


RETURNS_TABLE = "mirakl_returns"

# platform + shop_name match what offerprice_listing uses so a JOIN on
# (platform, shop_name, ...) just works across both tables.
STORE_CONFIGS: Dict[str, Dict[str, str]] = {
    "macy_kuyotq":   {"label": "Macy-Kuyotq",   "platform": "Macy",  "shop_name": "kuyotq"},
    "macy_wopet":    {"label": "Macy-Wopet",    "platform": "Macy",  "shop_name": "wopet"},
    "lowes_autool":  {"label": "Lowes-Autool",  "platform": "Lowes", "shop_name": "autool"},
    "lowes_yasonic": {"label": "Lowes-Yasonic", "platform": "Lowes", "shop_name": "yasonic"},
}


# RT11 supports `limit=N` (same as TL02). Default without limit is only 10/page
# - empirically verified. With limit=100 + 10 pages/run we absorb up to 1000
# returns per cron tick, far more than realistic daily volume.
RT11_PAGE_LIMIT = 100
MAX_PAGES_PER_RUN = 10
REQUEST_INTERVAL = 5         # seconds between calls
OVERLAP_HOURS = 2            # incremental overlap to catch late-updated rows
DEFAULT_BACKFILL_DAYS = 180  # if no cursor and no --full-from, look back 6 months


INSERT_COLUMNS = [
    "platform", "shop_name",
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


def _ensure_schema():
    """Create the single shared returns table + the cursor table if absent."""
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS order_system.`{RETURNS_TABLE}` (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    platform VARCHAR(50) NOT NULL,
                    shop_name VARCHAR(100) NOT NULL,
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
                    UNIQUE KEY uq_store_return (platform, shop_name, return_id),
                    INDEX idx_store_state (platform, shop_name, state),
                    INDEX idx_store_last_updated (platform, shop_name, last_updated),
                    INDEX idx_store_order (platform, shop_name, order_commercial_id),
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
            # idempotent column add: 4 columns for the web dashboard
            for col_def in [
                ("last_run_at", "DATETIME NULL"),
                ("last_records_fetched", "INT DEFAULT 0"),
                ("last_records_inserted", "INT DEFAULT 0"),
                ("last_status", "VARCHAR(64) NULL"),
            ]:
                cursor.execute("""
                    SELECT 1 FROM information_schema.columns
                     WHERE table_schema='order_system'
                       AND table_name='returns_sync_cursor'
                       AND column_name=%s LIMIT 1
                """, (col_def[0],))
                if not cursor.fetchone():
                    cursor.execute(
                        f"ALTER TABLE order_system.returns_sync_cursor "
                        f"ADD COLUMN {col_def[0]} {col_def[1]}"
                    )
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


def _save_cursor(store_key: str, last_synced_at: str,
                 records_fetched: int = 0, records_inserted: int = 0,
                 status: str = "completed"):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO order_system.returns_sync_cursor
                    (store_key, last_synced_at, last_run_at,
                     last_records_fetched, last_records_inserted, last_status)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    last_synced_at = VALUES(last_synced_at),
                    last_run_at = VALUES(last_run_at),
                    last_records_fetched = VALUES(last_records_fetched),
                    last_records_inserted = VALUES(last_records_inserted),
                    last_status = VALUES(last_status)
            """, (store_key, last_synced_at, now,
                  records_fetched, records_inserted, status))
        conn.commit()
    finally:
        conn.close()


def _save_run_failure(store_key: str, error_msg: str):
    """Write a 'failed' run-record even when nothing got inserted, so the
    dashboard can show the failure instead of an outdated 'completed' from
    yesterday."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO order_system.returns_sync_cursor
                    (store_key, last_synced_at, last_run_at,
                     last_records_fetched, last_records_inserted, last_status)
                VALUES (%s, NULL, %s, 0, 0, %s)
                ON DUPLICATE KEY UPDATE
                    last_run_at = VALUES(last_run_at),
                    last_records_fetched = 0,
                    last_records_inserted = 0,
                    last_status = VALUES(last_status)
            """, (store_key, now, f"failed: {error_msg[:200]}"))
        conn.commit()
    finally:
        conn.close()


def _api_to_db_row(item: Dict[str, Any], store_cfg: Dict[str, str]) -> Dict[str, Any]:
    addr = item.get("return_address") or {}
    tracking = item.get("tracking") or {}
    lines = item.get("return_lines") or []
    return {
        "platform": store_cfg["platform"],
        "shop_name": store_cfg["shop_name"],
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


def _insert_batch(conn, items: List[Dict[str, Any]], store_cfg: Dict[str, str]) -> int:
    if not items:
        return 0
    col_expr = ", ".join(f"`{c}`" for c in INSERT_COLUMNS)
    placeholders = ", ".join(["%s"] * len(INSERT_COLUMNS))
    sql = f"INSERT IGNORE INTO order_system.`{RETURNS_TABLE}` ({col_expr}) VALUES ({placeholders})"
    batch = []
    for item in items:
        row = _api_to_db_row(item, store_cfg)
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
    _ensure_schema()

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
                params = {"page_token": page_token, "limit": RT11_PAGE_LIMIT}
            else:
                params = {"return_last_updated_from": start_from, "limit": RT11_PAGE_LIMIT}

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
                inserted = _insert_batch(conn, data, store_cfg)
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

    if not error_msg:
        # Always update run-record on success (even when 0 rows: shows "still alive")
        _save_cursor(
            store_key,
            latest_updated or (cursor_val or ""),
            records_fetched=records_fetched,
            records_inserted=records_inserted,
            status="completed" if (not page_token) else "partial",
        )
    else:
        _save_run_failure(store_key, error_msg)

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


# =============================================================================
# Dashboard helpers - feed the /returns/sync web page
# =============================================================================

# Per-store cron minute, mirrored from server admin crontab. Used to compute
# "next sync at" on the dashboard so the operator does not have to remember
# the schedule.
CRON_MINUTE_BY_STORE: Dict[str, int] = {
    "macy_kuyotq":   7,
    "macy_wopet":    22,
    "lowes_autool":  42,
    "lowes_yasonic": 57,
}


def _next_cron_time(cron_minute: int, now: datetime) -> datetime:
    """Return the next datetime when minute==cron_minute strikes."""
    candidate = now.replace(minute=cron_minute, second=0, microsecond=0)
    if candidate <= now:
        candidate = candidate + timedelta(hours=1)
    return candidate


def get_sync_status_for_all_stores() -> List[Dict[str, Any]]:
    """Return per-store sync status for the dashboard page. One row per store
    even when the store has never synced (so the card always renders).
    """
    # ensure tables exist on a fresh deploy - cheap idempotent call
    _ensure_schema()

    conn = DBManager.get_connection()
    cursors: Dict[str, Dict[str, Any]] = {}
    totals: Dict[tuple, int] = {}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT store_key, last_synced_at, last_run_at,
                       last_records_fetched, last_records_inserted, last_status
                  FROM order_system.returns_sync_cursor
            """)
            for row in cur.fetchall():
                cursors[row["store_key"]] = row

            cur.execute(f"""
                SELECT platform, shop_name, COUNT(*) AS c
                  FROM order_system.`{RETURNS_TABLE}`
                 GROUP BY platform, shop_name
            """)
            for row in cur.fetchall():
                totals[(row["platform"], row["shop_name"])] = int(row["c"] or 0)
    finally:
        conn.close()

    now = datetime.now()
    status_list: List[Dict[str, Any]] = []
    for store_key, store_cfg in STORE_CONFIGS.items():
        c = cursors.get(store_key, {}) or {}
        total = totals.get((store_cfg["platform"], store_cfg["shop_name"]), 0)
        last_run_at = c.get("last_run_at")
        last_status = c.get("last_status") or "no_runs_yet"

        # Health: green if last successful run within the last 2 hours,
        # red otherwise (cron is hourly, so 2h missed = something wrong).
        health = "unknown"
        minutes_since = None
        if last_run_at:
            delta = (now - last_run_at).total_seconds()
            minutes_since = int(delta / 60)
            if last_status.startswith("failed"):
                health = "red"
            elif delta > 2 * 3600:
                health = "red"
            else:
                health = "green"

        next_cron = _next_cron_time(CRON_MINUTE_BY_STORE[store_key], now)
        minutes_until = int((next_cron - now).total_seconds() / 60)

        status_list.append({
            "store_key": store_key,
            "label": store_cfg["label"],
            "platform": store_cfg["platform"],
            "shop_name": store_cfg["shop_name"],
            "health": health,
            "last_run_at": last_run_at.strftime("%Y-%m-%d %H:%M:%S") if last_run_at else None,
            "minutes_since_run": minutes_since,
            "last_synced_at": c.get("last_synced_at"),
            "last_records_fetched": int(c.get("last_records_fetched") or 0),
            "last_records_inserted": int(c.get("last_records_inserted") or 0),
            "last_status": last_status,
            "total_returns": total,
            "next_cron_at": next_cron.strftime("%H:%M"),
            "minutes_until_next": minutes_until,
        })
    return status_list
