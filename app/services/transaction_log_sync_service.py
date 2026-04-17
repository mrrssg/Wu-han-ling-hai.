import hashlib
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
    "macy_kuyotq": {
        "label": "Macy-Kuyotq",
        "table": "macy_kuyotq_transaction_logs",
        "seller": "Kuyotq",
        "seller_id": "3793",
    },
    "macy_wopet": {
        "label": "Macy-Wopet",
        "table": "macy_wopet_transaction_logs",
        "seller": "Wopet",
        "seller_id": "3896",
    },
}

TYPE_MAP = {
    "ORDER_AMOUNT": "Order amount",
    "ORDER_AMOUNT_TAX": "Order amount tax",
    "OPERATOR_REMITTED_ORDER_AMOUNT_TAX": "Order amount tax remitted by operator",
    "ORDER_SHIPPING_AMOUNT": "Shipping charges",
    "ORDER_SHIPPING_AMOUNT_TAX": "Shipping tax",
    "OPERATOR_REMITTED_ORDER_SHIPPING_AMOUNT_TAX": "Shipping charges tax remitted by operator",
    "COMMISSION_FEE": "Commission",
    "COMMISSION_VAT": "Commission tax",
    "REFUND_ORDER_AMOUNT": "Order amount refund",
    "REFUND_ORDER_AMOUNT_TAX": "Order amount tax refund",
    "OPERATOR_REMITTED_REFUND_ORDER_AMOUNT_TAX": "Order amount tax remitted by operator refund",
    "REFUND_ORDER_SHIPPING_AMOUNT": "Shipping charge refund",
    "REFUND_ORDER_SHIPPING_AMOUNT_TAX": "Shipping tax refund",
    "OPERATOR_REMITTED_REFUND_ORDER_SHIPPING_AMOUNT_TAX": "Shipping charges tax remitted by operator refund",
    "REFUND_COMMISSION_FEE": "Commission refund",
    "REFUND_COMMISSION_VAT": "Commission tax refund",
    "MANUAL_CREDIT": "Manual credit",
    "MANUAL_CREDIT_VAT": "Manual credit tax",
    "MANUAL_INVOICE": "Manual invoice",
    "MANUAL_INVOICE_VAT": "Manual invoice tax",
    "PAYMENT": "Payment",
    "SELLER_RESERVE_FUND": "Seller reserve fund",
    "SELLER_RESERVE_SETTLEMENT": "Seller reserve settlement",
}

CANONICAL_COLUMNS = [
    "Date created",
    "Date received",
    "Transaction Date",
    "Seller",
    "Order number",
    "Invoice number",
    "Transaction Number",
    "Quantity",
    "Category Label",
    "Offer SKU",
    "Description",
    "Type",
    "Payment status",
    "Payment reference",
    "Amount",
    "Debit",
    "Credit",
    "Balance",
    "Currency",
    "Customer order reference",
    "Seller order reference",
    "Billing cycle date",
    "Seller ID",
    "Order line ID",
    "Refund ID",
    "Sales channel",
]

# TL02 official limits: max 20/min, 60/hour. We stay well under.
PAGE_LIMIT = 2000           # max allowed by API
MAX_PAGES_PER_RUN = 5       # 5 requests per cron run, well under 60/hour
REQUEST_INTERVAL = 10       # seconds between requests (≤6/min, well under 20/min)
OVERLAP_HOURS = 2           # overlap window to catch late-updated records


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso_utc(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _api_dt_to_csv_format(iso_str: Optional[str]) -> Optional[str]:
    if not iso_str:
        return None
    try:
        text = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        return dt.strftime("%m/%d/%Y %I:%M:%S %p")
    except Exception:
        return iso_str


def _payment_state_to_csv(state: Optional[str]) -> Optional[str]:
    mapping = {"PAID": "Paid", "PAYABLE": "Payable", "PENDING": "Pending"}
    return mapping.get(state or "", state)


def _amount_or_none(val) -> Optional[str]:
    if val is None:
        return None
    try:
        f = float(val)
        if f == 0.0:
            return None
        return f"{f:.2f}"
    except (ValueError, TypeError):
        return None


def _amount_str(val) -> Optional[str]:
    if val is None:
        return None
    try:
        return f"{float(val):.2f}"
    except (ValueError, TypeError):
        return None


def _row_fingerprint(row: Dict[str, str]) -> str:
    payload = "\x1f".join((row.get(col) or "") for col in CANONICAL_COLUMNS)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _api_record_to_db_row(item: Dict[str, Any], store_cfg: Dict[str, str]) -> Dict[str, str]:
    entities = item.get("entities") or {}
    order = entities.get("order") or {}
    order_line = entities.get("order_line") or {}
    refund = entities.get("refund") or {}

    api_type = item.get("type") or ""
    csv_type = TYPE_MAP.get(api_type, api_type)

    return {
        "Date created": _api_dt_to_csv_format(item.get("date_created")),
        "Date received": _api_dt_to_csv_format(item.get("last_updated")),
        "Transaction Date": None,
        "Seller": store_cfg["seller"],
        "Order number": order.get("id"),
        "Invoice number": item.get("accounting_document_number"),
        "Transaction Number": None,
        "Quantity": None,
        "Category Label": None,
        "Offer SKU": None,
        "Description": None,
        "Type": csv_type,
        "Payment status": _payment_state_to_csv(item.get("payment_state")),
        "Payment reference": None,
        "Amount": _amount_str(item.get("amount")),
        "Debit": _amount_or_none(item.get("amount_debited")),
        "Credit": _amount_or_none(item.get("amount_credited")),
        "Balance": _amount_str(item.get("balance")),
        "Currency": item.get("currency_iso_code"),
        "Customer order reference": None,
        "Seller order reference": None,
        "Billing cycle date": _api_dt_to_csv_format(item.get("accounting_document_creation_date")),
        "Seller ID": store_cfg["seller_id"],
        "Order line ID": order_line.get("id"),
        "Refund ID": refund.get("id"),
        "Sales channel": None,
    }


def _ensure_schema(table: str):
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT 1 FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA='order_system' AND TABLE_NAME=%s AND COLUMN_NAME='transaction_id'
            """, (table,))
            if not cursor.fetchone():
                cursor.execute(f"ALTER TABLE order_system.`{table}` ADD COLUMN `transaction_id` CHAR(36) NULL")
                cursor.execute(f"CREATE UNIQUE INDEX `uq_transaction_id` ON order_system.`{table}` (`transaction_id`)")

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS order_system.txn_sync_cursor (
                    store_key VARCHAR(64) PRIMARY KEY,
                    last_synced_at VARCHAR(64) NULL,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
        conn.commit()
    finally:
        conn.close()


def _load_last_synced_at(store_key: str) -> Optional[str]:
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT last_synced_at FROM order_system.txn_sync_cursor WHERE store_key = %s",
                (store_key,),
            )
            row = cursor.fetchone()
            return row["last_synced_at"] if row and row.get("last_synced_at") else None
    finally:
        conn.close()


def _save_last_synced_at(store_key: str, last_synced_at: str):
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO order_system.txn_sync_cursor (store_key, last_synced_at)
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE last_synced_at = VALUES(last_synced_at)
            """, (store_key, last_synced_at))
        conn.commit()
    finally:
        conn.close()


def _insert_batch(conn, table: str, items: List[Dict[str, Any]], store_cfg: Dict[str, str]) -> int:
    if not items:
        return 0
    col_expr = ", ".join(f"`{col}`" for col in CANONICAL_COLUMNS)
    col_expr += ", `row_fingerprint`, `transaction_id`"
    placeholders = ", ".join(["%s"] * (len(CANONICAL_COLUMNS) + 2))
    insert_sql = f"INSERT IGNORE INTO order_system.`{table}` ({col_expr}) VALUES ({placeholders})"

    batch = []
    for item in items:
        db_row = _api_record_to_db_row(item, store_cfg)
        fingerprint = _row_fingerprint(db_row)
        transaction_id = item.get("id")
        values = tuple((db_row.get(col) or "") if db_row.get(col) else None for col in CANONICAL_COLUMNS)
        values += (fingerprint, transaction_id)
        batch.append(values)

    with conn.cursor() as cursor:
        cursor.executemany(insert_sql, batch)
        inserted = cursor.rowcount or 0
    conn.commit()
    return inserted


def run_transaction_log_sync(
    store_key: str,
    max_pages: int = MAX_PAGES_PER_RUN,
) -> Dict[str, Any]:
    """
    Incremental sync using TL02 with last_updated_from filter.
    - Uses sort=lastUpdated,ASC + limit=2000 for maximum efficiency
    - Saves last_updated timestamp for next run
    - Each run makes at most max_pages requests (default 5)
    - Respects TL02 rate limits: max 20/min, 60/hour
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

    # Determine start point
    last_synced = _load_last_synced_at(store_key)
    if last_synced:
        # Parse and subtract overlap to catch late-updated records
        try:
            dt = datetime.fromisoformat(last_synced.replace("Z", "+00:00"))
            start_from = _iso_utc(dt - timedelta(hours=OVERLAP_HOURS))
        except Exception:
            start_from = last_synced
    else:
        # First run: no previous sync, start from very beginning
        start_from = "2024-01-01T00:00:00Z"

    print(f"[TXN_SYNC][{store_key}] last_synced={last_synced}, start_from={start_from}")

    pages_fetched = 0
    records_fetched = 0
    records_inserted = 0
    records_duplicate = 0
    latest_updated = last_synced or ""
    error_msg = ""
    page_token = None

    conn = DBManager.get_connection()
    try:
        while pages_fetched < max_pages:
            if pages_fetched > 0:
                time.sleep(REQUEST_INTERVAL)

            if page_token:
                params = {"page_token": page_token}
            else:
                params = {
                    "limit": PAGE_LIMIT,
                    "last_updated_from": start_from,
                    "sort": "lastUpdated,ASC",
                }

            try:
                resp = _request_with_retry(
                    method="GET",
                    url=f"{api_url.rstrip('/')}/api/sellerpayment/transactions_logs",
                    headers=headers,
                    params=params,
                    proxies=network["proxies"],
                    timeout=60,
                )
            except Exception as e:
                error_msg = f"request failed: {e}"
                break

            if resp.status_code != 200:
                error_msg = f"API returned {resp.status_code}: {resp.text[:500]}"
                break

            try:
                payload = resp.json()
            except Exception as e:
                error_msg = f"invalid json: {e}"
                break

            data = payload.get("data") or []
            page_token = payload.get("next_page_token")
            pages_fetched += 1
            records_fetched += len(data)

            if data:
                inserted = _insert_batch(conn, table, data, store_cfg)
                records_inserted += inserted
                records_duplicate += len(data) - inserted

                # Track the latest last_updated for cursor
                for item in data:
                    lu = item.get("last_updated") or ""
                    if lu > latest_updated:
                        latest_updated = lu

            first_date = data[0].get("last_updated", "")[:10] if data else "-"
            last_date = data[-1].get("last_updated", "")[:10] if data else "-"
            print(f"[TXN_SYNC][{store_key}] page={pages_fetched}, fetched={len(data)}, inserted={records_inserted}, dup={records_duplicate}, range={first_date}~{last_date}")

            if not page_token or not data:
                break

    except Exception as e:
        conn.rollback()
        error_msg = str(e)
    finally:
        conn.close()

    # Save cursor
    if latest_updated and not error_msg:
        _save_last_synced_at(store_key, latest_updated)

    success = not error_msg
    reached_end = not page_token
    msg = error_msg if error_msg else ("sync completed - all caught up" if reached_end else f"sync completed - more pages available, will continue next run")
    result = {
        "success": success,
        "store_key": store_key,
        "msg": msg,
        "reached_end": reached_end,
        "pages_fetched": pages_fetched,
        "records_fetched": records_fetched,
        "records_inserted": records_inserted,
        "records_duplicate": records_duplicate,
        "latest_updated": latest_updated,
    }
    print(f"[TXN_SYNC][{store_key}] done: {result}")
    return result
