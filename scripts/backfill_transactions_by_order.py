"""
Backfill Mirakl transaction logs for 2026 orders whose CSV import got truncated
by fingerprint dedup. Fetches per-order transaction history via TL02 and replaces
legacy CSV rows (transaction_id IS NULL) with complete API rows.

Scope: 2026 only. Does not touch cron cursor or pre-2026 rows.
IP-isolated per store via shop_configs.

Safe by design:
  - Per-batch atomic transaction (INSERT IGNORE + DELETE in one commit).
  - Orders whose API returned 0 rows are skipped (CSV rows preserved).
  - DELETE is limited to 2026 rows to keep pre-2026 CSV data intact.

Usage:
    python scripts/backfill_transactions_by_order.py --store macy_kuyotq
    python scripts/backfill_transactions_by_order.py --store macy_kuyotq --dry-run
    python scripts/backfill_transactions_by_order.py --store macy_kuyotq --limit-batches 1
    python scripts/backfill_transactions_by_order.py --store macy_kuyotq --resume-from 4721839459-A
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List

# Allow direct invocation (python scripts/backfill_transactions_by_order.py)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app import create_app
from app.models.db_manager import DBManager
from app.services.mirakl_shipping_service import (
    _load_network_profile,
    _request_with_retry,
    load_store_config,
)
from app.services.transaction_log_sync_service import (
    CANONICAL_COLUMNS,
    STORE_CONFIGS,
    _api_record_to_db_row,
)


DEFAULT_BATCH_SIZE = 50       # order_id per TL02 call; URL ≈ 550B for 10-char IDs
# Mirakl TL02 hard limits: 20/min AND 60/hour. The 60/hour (= 1/min sustained)
# is the binding constraint for long-running backfills. At 75s between calls
# we stay at ~48/hour, well below the ceiling. Exceeding 60/hour risks
# triggering API-key suspension which requires manual support to unblock —
# do NOT lower this value.
REQUEST_INTERVAL = 75
PAGE_LIMIT = 2000             # TL02 max
TARGET_YEAR = "2026"          # scope
REQUEST_TIMEOUT = 60
REQUEST_RETRIES = 3


def _log(msg: str, log_file=None) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if log_file:
        log_file.write(line + "\n")
        log_file.flush()


def _fetch_affected_orders(store_key: str) -> List[str]:
    """2026 orders that still carry CSV-legacy rows and are likely bug candidates.

    Includes: qty>=2 orders (known to be truncated) + orders missing from
    autooperate.macyorder (unknown qty, handled conservatively).
    Excludes: qty=1 orders from macyorder (CSV single row is already correct),
    and Mirakl manual-invoice placeholders ("NA", empty) which don't have real
    order_ids to query TL02 with. Real order_ids always start with a digit.
    """
    store_cfg = STORE_CONFIGS[store_key]
    table = store_cfg["table"]
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT DISTINCT t.`Order number` AS order_id
                FROM order_system.`{table}` t
                LEFT JOIN autooperate.macyorder o
                       ON o.`Order number` = t.`Order number`
                WHERE t.transaction_id IS NULL
                  AND SUBSTRING(t.`Date created`, 7, 4) = %s
                  AND t.`Order number` IS NOT NULL
                  AND t.`Order number` <> ''
                  AND t.`Order number` REGEXP '^[0-9]'
                  AND (o.Quantity >= 2 OR o.`Order number` IS NULL)
                ORDER BY t.`Order number`
                """,
                (TARGET_YEAR,),
            )
            rows = cursor.fetchall()
    finally:
        conn.close()
    return [r["order_id"] for r in rows if r.get("order_id")]


def _fetch_transactions_for_orders(
    order_ids: List[str], api_cfg: Dict, network: Dict, log_file
) -> List[Dict]:
    """TL02 call with multi-value order_id param, paging to end via next_page_token."""
    headers = {
        "Authorization": api_cfg["api_key"],
        "Accept": "application/json",
        "User-Agent": network["user_agent"],
        "Connection": "close",
    }
    url = f"{api_cfg['api_url'].rstrip('/')}/api/sellerpayment/transactions_logs"

    all_items: List[Dict] = []
    page_token = None
    page_no = 0
    while True:
        if page_token:
            params = {"page_token": page_token}
        else:
            # requests expands list values into repeated query params: order_id=A&order_id=B
            params = {
                "limit": PAGE_LIMIT,
                "order_id": order_ids,
                "sort": "dateCreated,ASC",
            }

        resp = _request_with_retry(
            method="GET",
            url=url,
            headers=headers,
            params=params,
            proxies=network["proxies"],
            timeout=REQUEST_TIMEOUT,
            retries=REQUEST_RETRIES,
            backoff=2.0,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"TL02 {resp.status_code}: {resp.text[:400]}")

        try:
            payload = resp.json()
        except ValueError as exc:
            raise RuntimeError(f"TL02 non-JSON: {exc}") from exc

        data = payload.get("data") or []
        all_items.extend(data)
        page_no += 1
        page_token = payload.get("next_page_token")
        _log(
            f"    page {page_no}: got {len(data)} (total {len(all_items)}), "
            f"next={'y' if page_token else 'n'}",
            log_file,
        )

        if not page_token or not data:
            break
        time.sleep(REQUEST_INTERVAL)

    return all_items


def _filter_2026_items(items: List[Dict]) -> List[Dict]:
    out = []
    for item in items:
        dc = (item.get("date_created") or "")
        if dc.startswith(TARGET_YEAR):
            out.append(item)
    return out


def _group_by_order(items: List[Dict]) -> Dict[str, List[Dict]]:
    by_order: Dict[str, List[Dict]] = defaultdict(list)
    for item in items:
        oid = ((item.get("entities") or {}).get("order") or {}).get("id")
        if oid:
            by_order[str(oid)].append(item)
    return by_order


def _apply_batch(
    store_key: str,
    batch_order_ids: List[str],
    items_to_insert: List[Dict],
    orders_to_delete: List[str],
) -> Dict[str, int]:
    """Atomic per-batch: INSERT IGNORE the API rows, then DELETE legacy CSV rows.

    INSERT first so that if DELETE fails the data is still represented; commit
    both together so the table never shows a transient empty state for an order.
    """
    store_cfg = STORE_CONFIGS[store_key]
    table = store_cfg["table"]

    col_expr = ", ".join(f"`{col}`" for col in CANONICAL_COLUMNS) + ", `transaction_id`"
    placeholders = ", ".join(["%s"] * (len(CANONICAL_COLUMNS) + 1))
    insert_sql = (
        f"INSERT IGNORE INTO order_system.`{table}` ({col_expr}) VALUES ({placeholders})"
    )

    insert_values = []
    for item in items_to_insert:
        db_row = _api_record_to_db_row(item, store_cfg)
        values = tuple((db_row.get(c) or "") if db_row.get(c) else None for c in CANONICAL_COLUMNS)
        values += (item.get("id"),)
        insert_values.append(values)

    inserted = 0
    deleted = 0
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            if insert_values:
                cursor.executemany(insert_sql, insert_values)
                inserted = cursor.rowcount or 0

            if orders_to_delete:
                del_placeholders = ", ".join(["%s"] * len(orders_to_delete))
                cursor.execute(
                    f"""
                    DELETE FROM order_system.`{table}`
                    WHERE transaction_id IS NULL
                      AND `Order number` IN ({del_placeholders})
                      AND SUBSTRING(`Date created`, 7, 4) = %s
                    """,
                    tuple(orders_to_delete) + (TARGET_YEAR,),
                )
                deleted = cursor.rowcount or 0

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {"inserted": inserted, "deleted": deleted}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill 2026 Mirakl transaction logs per order via TL02."
    )
    parser.add_argument("--store", required=True, choices=list(STORE_CONFIGS.keys()))
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the affected order count and first batch, then exit",
    )
    parser.add_argument(
        "--limit-batches",
        type=int,
        default=None,
        help="stop after N API batches (for testing; overrides full run)",
    )
    parser.add_argument(
        "--resume-from",
        type=str,
        default=None,
        help="skip orders up to and including this order_id (for restart)",
    )
    args = parser.parse_args()

    store_key = args.store

    base_dir_guess = Path(__file__).resolve().parent.parent
    log_dir = base_dir_guess / "logs"
    log_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"backfill_{store_key}_{stamp}.log"

    config_name = os.environ.get("FLASK_CONFIG", "production")
    app = create_app(config_name)
    with app.app_context(), open(log_path, "w", encoding="utf-8") as lf:
        _log(f"=== backfill {store_key} starting ===", lf)
        _log(f"log: {log_path}", lf)

        orders = _fetch_affected_orders(store_key)
        _log(f"affected orders (2026 qty>=2 OR not-in-macyorder): {len(orders)}", lf)

        if args.resume_from:
            try:
                idx = orders.index(args.resume_from)
                skipped = idx + 1
                orders = orders[skipped:]
                _log(f"resume: skipped {skipped} orders up to {args.resume_from}", lf)
            except ValueError:
                _log(
                    f"resume-from={args.resume_from} not in list; starting from top",
                    lf,
                )

        if args.dry_run:
            _log(f"DRY-RUN: first 5 orders = {orders[:5]}", lf)
            _log(f"DRY-RUN: last 5 orders = {orders[-5:] if len(orders) > 5 else orders}", lf)
            _log("DRY-RUN: exiting before any API call or DB write", lf)
            return 0

        if not orders:
            _log("nothing to backfill", lf)
            return 0

        base_dir = app.config.get("BASE_DIR", app.root_path)
        api_cfg = load_store_config(base_dir, store_key)
        if not api_cfg.get("api_key"):
            _log(f"ABORT: missing api key (instance/{store_key}_key.txt)", lf)
            return 1
        network = _load_network_profile(store_key)
        _log(
            f"network: proxy={network['proxy_ip']}:{network['proxy_port']} "
            f"platform={network['platform']} shop={network['shop_name']}",
            lf,
        )

        total_fetched = 0
        total_2026 = 0
        total_inserted = 0
        total_deleted = 0
        batches_done = 0
        orders_skipped_empty: List[str] = []
        failed_batches: List[Dict] = []

        for i in range(0, len(orders), args.batch_size):
            if args.limit_batches is not None and batches_done >= args.limit_batches:
                _log(f"reached --limit-batches={args.limit_batches}; stopping", lf)
                break

            if batches_done > 0:
                time.sleep(REQUEST_INTERVAL)

            batch = orders[i : i + args.batch_size]
            batches_done += 1
            _log(
                f"--- batch {batches_done} [{i + 1}-{i + len(batch)} / {len(orders)}] "
                f"first={batch[0]} last={batch[-1]}",
                lf,
            )

            try:
                items = _fetch_transactions_for_orders(batch, api_cfg, network, lf)
            except Exception as exc:
                _log(f"  !! TL02 failed for batch {batches_done}: {exc}", lf)
                failed_batches.append({"batch_no": batches_done, "orders": batch, "error": str(exc)})
                continue

            total_fetched += len(items)
            items_2026 = _filter_2026_items(items)
            total_2026 += len(items_2026)

            by_order = _group_by_order(items_2026)
            orders_with_data = set(by_order.keys())
            orders_to_delete = [o for o in batch if o in orders_with_data]
            orders_missing = [o for o in batch if o not in orders_with_data]
            if orders_missing:
                orders_skipped_empty.extend(orders_missing)
                _log(
                    f"  ?? API returned 0 (2026) rows for {len(orders_missing)} "
                    f"orders; keeping CSV rows. e.g. {orders_missing[:3]}",
                    lf,
                )

            items_to_insert = []
            for oid in orders_to_delete:
                items_to_insert.extend(by_order.get(oid, []))

            try:
                res = _apply_batch(store_key, batch, items_to_insert, orders_to_delete)
            except Exception as exc:
                _log(f"  !! DB apply failed for batch {batches_done}: {exc}", lf)
                failed_batches.append({"batch_no": batches_done, "orders": batch, "error": f"db: {exc}"})
                continue

            total_inserted += res["inserted"]
            total_deleted += res["deleted"]
            _log(
                f"  -> fetched={len(items)} (2026={len(items_2026)}), "
                f"deleted_csv={res['deleted']}, inserted_api={res['inserted']}, "
                f"orders_with_data={len(orders_with_data)}, empty={len(orders_missing)}",
                lf,
            )

        _log("=== backfill done ===", lf)
        _log(f"batches: {batches_done}", lf)
        _log(f"fetched={total_fetched} (2026={total_2026})", lf)
        _log(f"deleted_csv={total_deleted}, inserted_api={total_inserted}", lf)
        _log(f"orders skipped (empty API response): {len(orders_skipped_empty)}", lf)

        if orders_skipped_empty:
            path = log_dir / f"backfill_{store_key}_skipped_{stamp}.json"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(orders_skipped_empty, f)
            _log(f"skipped orders written to {path}", lf)

        if failed_batches:
            path = log_dir / f"backfill_{store_key}_failed_{stamp}.json"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(failed_batches, f)
            _log(f"failed batches written to {path}", lf)
            return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
