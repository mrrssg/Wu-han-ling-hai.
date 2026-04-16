import hashlib
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

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
        "seller_id": "",
    },
}

# API type -> CSV type mapping
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

JITTER_MIN = 5
JITTER_MAX = 30
OVERLAP_HOURS = 24


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso_utc(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _api_dt_to_csv_format(iso_str: Optional[str]) -> Optional[str]:
    """Convert '2024-12-23T12:43:55.666Z' -> '12/23/2024 07:43:55 AM' (ET-like, but we keep UTC)"""
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
    """Return formatted amount string, or None if 0."""
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
    """Map one API JSON record to a DB row dict matching CANONICAL_COLUMNS."""
    entities = item.get("entities") or {}
    order = entities.get("order") or {}
    order_line = entities.get("order_line") or {}
    refund = entities.get("refund") or {}

    api_type = item.get("type") or ""
    csv_type = TYPE_MAP.get(api_type, api_type)

    amount = item.get("amount")
    credited = item.get("amount_credited")
    debited = item.get("amount_debited")

    row = {
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
        "Amount": _amount_str(amount),
        "Debit": _amount_or_none(debited),
        "Credit": _amount_or_none(credited),
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
    return row


def _get_latest_date_in_db(table: str) -> Optional[datetime]:
    """Find the latest Date created in the table, return as UTC datetime."""
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"""
                SELECT `Date created` FROM order_system.`{table}`
                WHERE `Date created` IS NOT NULL AND `Date created` != ''
                ORDER BY
                    CAST(SUBSTRING_INDEX(SUBSTRING_INDEX(`Date created`, '/', 3), '/', -1) AS UNSIGNED) DESC,
                    CAST(SUBSTRING_INDEX(`Date created`, '/', 1) AS UNSIGNED) DESC,
                    CAST(SUBSTRING_INDEX(SUBSTRING_INDEX(`Date created`, '/', 2), '/', -1) AS UNSIGNED) DESC
                LIMIT 1
            """)
            row = cursor.fetchone()
            if not row or not row.get("Date created"):
                return None
            text = row["Date created"]
            try:
                return datetime.strptime(text, "%m/%d/%Y %I:%M:%S %p").replace(tzinfo=UTC)
            except ValueError:
                return None
    finally:
        conn.close()


def _ensure_transaction_id_column(table: str):
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
        conn.commit()
    finally:
        conn.close()


def run_transaction_log_sync(
    store_key: str,
    max_per_page: int = 100,
) -> Dict[str, Any]:
    if store_key not in STORE_CONFIGS:
        return {"success": False, "msg": f"unsupported store: {store_key}"}

    store_cfg = STORE_CONFIGS[store_key]
    table = store_cfg["table"]

    _ensure_transaction_id_column(table)

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

    # Determine start_date: latest in DB minus overlap
    latest_in_db = _get_latest_date_in_db(table)
    if latest_in_db:
        start_date = latest_in_db - timedelta(hours=OVERLAP_HOURS)
    else:
        start_date = datetime(2024, 1, 1, tzinfo=UTC)

    start_iso = _iso_utc(start_date)
    print(f"[TXN_SYNC][{store_key}] start_date={start_iso}, latest_in_db={latest_in_db}")

    pages_fetched = 0
    records_fetched = 0
    records_inserted = 0
    records_duplicate = 0
    page_token = None
    error_msg = ""

    col_expr = ", ".join(f"`{col}`" for col in CANONICAL_COLUMNS)
    col_expr += ", `row_fingerprint`, `transaction_id`"
    placeholders = ", ".join(["%s"] * (len(CANONICAL_COLUMNS) + 2))
    insert_sql = f"INSERT IGNORE INTO order_system.`{table}` ({col_expr}) VALUES ({placeholders})"

    conn = DBManager.get_connection()
    try:
        while True:
            jitter = random.randint(JITTER_MIN, JITTER_MAX)
            time.sleep(jitter)

            params = {"max": max_per_page, "start_date": start_iso}
            if page_token:
                params["page_token"] = page_token

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
            pages_fetched += 1
            records_fetched += len(data)

            if data:
                batch = []
                for item in data:
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
                records_inserted += inserted
                records_duplicate += len(batch) - inserted

            print(f"[TXN_SYNC][{store_key}] page={pages_fetched}, fetched={len(data)}, inserted_total={records_inserted}")

            page_token = payload.get("next_page_token")
            if not page_token or not data:
                break

    except Exception as e:
        conn.rollback()
        error_msg = str(e)
    finally:
        conn.close()

    success = not error_msg
    msg = error_msg if error_msg else "sync completed"
    result = {
        "success": success,
        "store_key": store_key,
        "msg": msg,
        "start_date": start_iso,
        "pages_fetched": pages_fetched,
        "records_fetched": records_fetched,
        "records_inserted": records_inserted,
        "records_duplicate": records_duplicate,
    }
    print(f"[TXN_SYNC][{store_key}] done: {result}")
    return result
