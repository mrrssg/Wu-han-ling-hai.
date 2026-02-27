import csv
import os
import time
from typing import Dict, List, Tuple
from urllib.parse import quote

import requests

from app.models.db_manager import DBManager


DEFAULT_MIRAKL_URL = "https://macysus-prod.mirakl.net"
FAIL_EXPORT_DIR = os.path.join(os.getcwd(), "exports", "mirakl_failures")
os.makedirs(FAIL_EXPORT_DIR, exist_ok=True)

STORE_NETWORK_RULES = {
    "macy_kuyotq": {"platform": "macys-kuyotq", "shop_name": "kuyotq"},
    "macy_wopet": {"platform": "macys-wopet", "shop_name": "wopet"},
    "bestbuy_delphi": {"platform": "bestbuy-delphi", "shop_name": "delphi"},
}

STORE_ORDER_TABLES = {
    "macy_kuyotq": "macyorder",
    "macy_wopet": "macyorder",
    "bestbuy_delphi": "bestbuyorder",
}

# Preview is user-facing and should return quickly.
PREVIEW_PAGE_DELAY_SECONDS = 0.2
PREVIEW_REQUEST_TIMEOUT_SECONDS = 30
PREVIEW_REQUEST_RETRIES = 1


def _read_text(path: str) -> str:
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def load_store_config(base_dir: str, store_key: str) -> Dict[str, str]:
    env_key = os.environ.get(f"MIRAKL_{store_key.upper()}_API_KEY", "").strip()
    env_url = os.environ.get(f"MIRAKL_{store_key.upper()}_API_URL", "").strip()

    key_path = os.path.join(base_dir, "instance", f"{store_key}_key.txt")
    url_path = os.path.join(base_dir, "instance", f"{store_key}_url.txt")

    api_key = env_key or _read_text(key_path)
    api_url = env_url or _read_text(url_path)

    if not api_url:
        api_url = DEFAULT_MIRAKL_URL

    return {"api_key": api_key, "api_url": api_url}


def _norm(value: str) -> str:
    return str(value or "").strip().lower()


def _load_network_profile(store_key: str) -> Dict[str, str]:
    store_key_norm = _norm(store_key)
    rule = STORE_NETWORK_RULES.get(store_key_norm)
    if not rule:
        raise RuntimeError(f"Store '{store_key}' is not configured for Mirakl IP isolation")

    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            sql = """
                SELECT platform, shop_name, proxy_ip, proxy_port, proxy_user, proxy_pass, user_agent, is_active
                FROM order_system.shop_configs
                WHERE LOWER(TRIM(platform)) = %s
                  AND LOWER(TRIM(shop_name)) = %s
                LIMIT 2
            """
            cursor.execute(sql, (rule["platform"], rule["shop_name"]))
            rows = cursor.fetchall()
    finally:
        conn.close()

    if not rows:
        raise RuntimeError(
            f"No shop_configs row for store '{store_key_norm}' "
            f"(platform={rule['platform']}, shop_name={rule['shop_name']})"
        )
    if len(rows) > 1:
        raise RuntimeError(
            f"Duplicate shop_configs rows for store '{store_key_norm}' "
            f"(platform={rule['platform']}, shop_name={rule['shop_name']})"
        )

    row = rows[0]
    if int(row.get("is_active") or 0) != 1:
        raise RuntimeError(f"shop_configs row is inactive for store '{store_key_norm}'")

    required = ("proxy_ip", "proxy_port", "proxy_user", "proxy_pass", "user_agent")
    missing = [k for k in required if not str(row.get(k) or "").strip()]
    if missing:
        raise RuntimeError(
            f"shop_configs missing required fields for '{store_key_norm}': {', '.join(missing)}"
        )

    proxy_ip = str(row["proxy_ip"]).strip()
    proxy_port = str(row["proxy_port"]).strip()
    proxy_user = quote(str(row["proxy_user"]).strip(), safe="")
    proxy_pass = quote(str(row["proxy_pass"]).strip(), safe="")
    user_agent = str(row["user_agent"]).strip()

    proxy_url = f"http://{proxy_user}:{proxy_pass}@{proxy_ip}:{proxy_port}"
    return {
        "store_key": store_key_norm,
        "platform": str(row.get("platform") or "").strip(),
        "shop_name": str(row.get("shop_name") or "").strip(),
        "proxy_ip": proxy_ip,
        "proxy_port": proxy_port,
        "user_agent": user_agent,
        "proxies": {"http": proxy_url, "https": proxy_url},
    }


def determine_carrier(tracking_number: str) -> str:
    if not tracking_number:
        return "fedex"
    t = str(tracking_number).split(",")[0].strip().upper()
    if t.startswith("D1"):
        return "ontrac"
    if t.startswith("1Z"):
        return "ups"
    return "fedex"


def fetch_unshipped_orders(api_url: str, api_key: str, store_key: str) -> List[Dict]:
    network_profile = _load_network_profile(store_key)

    all_rows: List[Dict] = []
    offset = 0
    limit = 100
    headers = {
        "Authorization": api_key,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "close",
        "User-Agent": network_profile["user_agent"],
    }

    while True:
        # Keep a tiny page gap to avoid burst, but do not block UI for tens of seconds.
        if offset > 0 and PREVIEW_PAGE_DELAY_SECONDS > 0:
            time.sleep(PREVIEW_PAGE_DELAY_SECONDS)

        params = {
            "order_state_codes": "WAITING_ACCEPTANCE,SHIPPING",
            "max": limit,
            "offset": offset,
            "paginate": "true",
        }

        resp = _request_with_retry(
            method="GET",
            url=f"{api_url.rstrip('/')}/api/orders",
            headers=headers,
            params=params,
            proxies=network_profile["proxies"],
            timeout=PREVIEW_REQUEST_TIMEOUT_SECONDS,
            retries=PREVIEW_REQUEST_RETRIES,
            backoff=0.5,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Mirakl request failed: {resp.status_code} {resp.text}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise RuntimeError(f"Mirakl returned non-JSON response: {exc}") from exc
        orders = data.get("orders", [])
        total_count = data.get("total_count", 0)
        if not orders:
            break

        for order in orders:
            order_id = order.get("order_id")
            for line in order.get("order_lines", []):
                all_rows.append(
                    {
                        "order_id": str(order_id or "").strip(),
                        "line_id": str(line.get("order_line_id") or "").strip(),
                        "sku": str(line.get("offer_sku") or "").strip(),
                        "qty": int(line.get("quantity") or 0),
                    }
                )

        if (offset + len(orders)) >= total_count:
            break
        offset += limit

    return all_rows


def _find_tracking(conn, order_table: str, order_id: str, line_id: str) -> str:
    with conn.cursor() as cursor:
        sql = f"""
            SELECT Tracking
            FROM {order_table}
            WHERE `Order number` LIKE %s AND `Order line no.` = %s
            ORDER BY `Order number` ASC
            LIMIT 1
        """
        cursor.execute(sql, (f"%{order_id}%", line_id))
        row = cursor.fetchone()
        if not row:
            return ""
        return str(row.get("Tracking") or "").strip()


def build_shipments(rows: List[Dict], store_key: str) -> Tuple[List[Dict], Dict]:
    store_key_norm = _norm(store_key)
    order_table = STORE_ORDER_TABLES.get(store_key_norm)
    if not order_table:
        raise RuntimeError(f"Store '{store_key}' is not configured for order-table mapping")

    shipments: List[Dict] = []
    stats = {
        "total_lines": len(rows),
        "with_tracking": 0,
        "shipments": 0,
    }

    conn = DBManager.get_connection()
    try:
        for row in rows:
            order_id = row.get("order_id", "")
            line_id = row.get("line_id", "")
            sku = row.get("sku", "")
            qty = int(row.get("qty") or 0)

            tracking_raw = _find_tracking(conn, order_table, order_id, line_id)
            tracking_list = [t.strip() for t in str(tracking_raw).split(",") if t.strip()]

            if tracking_list:
                stats["with_tracking"] += 1

            if len(tracking_list) > 1 and qty == 1:
                tracking = tracking_list[0]
                shipments.append(
                    {
                        "order_id": order_id,
                        "line_id": line_id,
                        "sku": sku,
                        "qty": qty,
                        "tracking": tracking,
                        "carrier": determine_carrier(tracking),
                        "can_ship": bool(tracking),
                        "note": "multiple tracking numbers found while qty=1",
                    }
                )
            elif len(tracking_list) > 1 and qty >= 2:
                n = len(tracking_list)
                avg, rem = divmod(qty, n)
                for i, t_num in enumerate(tracking_list):
                    ship_qty = avg + (1 if i < rem else 0)
                    if ship_qty <= 0:
                        continue
                    shipments.append(
                        {
                            "order_id": order_id,
                            "line_id": line_id,
                            "sku": sku,
                            "qty": ship_qty,
                            "tracking": t_num,
                            "carrier": determine_carrier(t_num),
                            "can_ship": True,
                            "note": "",
                        }
                    )
            else:
                tracking = tracking_list[0] if tracking_list else ""
                shipments.append(
                    {
                        "order_id": order_id,
                        "line_id": line_id,
                        "sku": sku,
                        "qty": qty,
                        "tracking": tracking,
                        "carrier": determine_carrier(tracking),
                        "can_ship": bool(tracking),
                        "note": "" if tracking else "missing tracking",
                    }
                )

        stats["shipments"] = len(shipments)
        return shipments, stats
    finally:
        conn.close()


def submit_shipments(
    api_url: str,
    api_key: str,
    shipments: List[Dict],
    batch_size: int = 500,
    store_key: str = "macy",
) -> Dict:
    network_profile = _load_network_profile(store_key)
    headers = {
        "Authorization": api_key,
        "Content-Type": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "close",
        "User-Agent": network_profile["user_agent"],
    }
    max_batch = max(1, min(int(batch_size or 500), 1000))
    success_all = []
    errors_all = []
    batch_results = []

    for i in range(0, len(shipments), max_batch):
        chunk = shipments[i : i + max_batch]
        payload = {"shipments": []}
        for s in chunk:
            payload["shipments"].append(
                {
                    "order_id": s["order_id"],
                    "shipped": True,
                    "tracking": {
                        "carrier_code": str(s["carrier"]).strip().lower(),
                        "tracking_number": s["tracking"],
                    },
                    "shipment_lines": [
                        {
                            "order_line_id": s["line_id"],
                            "offer_sku": s["sku"],
                            "quantity": int(s["qty"]),
                        }
                    ],
                }
            )

        resp = _request_with_retry(
            method="POST",
            url=f"{api_url.rstrip('/')}/api/shipments",
            headers=headers,
            json=payload,
            proxies=network_profile["proxies"],
            timeout=60,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Mirakl shipment failed: {resp.status_code} {resp.text}")

        result = resp.json()
        chunk_success = result.get("shipment_success", [])
        chunk_errors = result.get("shipment_errors", [])
        success_all.extend(chunk_success)
        errors_all.extend(chunk_errors)
        batch_results.append(
            {
                "batch_index": (i // max_batch) + 1,
                "batch_size": len(chunk),
                "success": len(chunk_success),
                "errors": len(chunk_errors),
            }
        )
        time.sleep(0.2)

    failure_csv = ""
    if errors_all:
        ts = time.strftime("%Y%m%d_%H%M%S")
        filename = f"{store_key}_failures_{ts}.csv"
        failure_csv = os.path.join(FAIL_EXPORT_DIR, filename)
        with open(failure_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["order_id", "message"])
            for e in errors_all:
                writer.writerow([e.get("order_id", ""), e.get("message", "")])

    return {
        "shipment_success": success_all,
        "shipment_errors": errors_all,
        "batch_size": max_batch,
        "batches": batch_results,
        "failure_csv": failure_csv,
    }


def _request_with_retry(method: str, url: str, retries: int = 3, backoff: float = 1.0, **kwargs):
    for attempt in range(retries + 1):
        try:
            with requests.Session() as session:
                session.trust_env = False
                resp = session.request(method=method, url=url, **kwargs)
            if resp.status_code in (429, 503):
                if attempt < retries:
                    time.sleep(backoff * (2**attempt))
                    continue
            return resp
        except requests.RequestException:
            if attempt >= retries:
                raise
            time.sleep(backoff * (2**attempt))
    raise RuntimeError("Request failed after retries")
