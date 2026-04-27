import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from flask import current_app

from app.models.db_manager import DBManager
from app.services.mirakl_shipping_service import (
    _load_network_profile,
    _request_with_retry,
    load_store_config,
)

PAGE_SIZE = 100
PAGE_DELAY_SECONDS = 1.5
REQUEST_TIMEOUT = 60
REQUEST_RETRIES = 3

STORE_CONFIGS: Dict[str, Dict[str, str]] = {
    "macy_kuyotq": {"label": "Macy-Kuyotq", "platform": "Macy", "shop_name": "kuyotq"},
    "macy_wopet":  {"label": "Macy-Wopet",  "platform": "Macy", "shop_name": "wopet"},
}


def _norm(value: Any) -> str:
    return str(value or "").strip()


def _parse_datetime(value: Any) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip().rstrip("Z").replace("T", " ")[:19]
    try:
        datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        return text
    except ValueError:
        return None


def _fetch_all_offers(api_url: str, api_key: str, network_profile: Dict) -> List[Dict]:
    headers = {
        "Authorization": api_key,
        "Accept": "application/json",
        "User-Agent": network_profile["user_agent"],
        "Connection": "close",
    }
    all_offers: List[Dict] = []
    offset = 0

    while True:
        if offset > 0:
            time.sleep(PAGE_DELAY_SECONDS)

        params = {"max": PAGE_SIZE, "offset": offset}
        resp = _request_with_retry(
            method="GET",
            url=f"{api_url.rstrip('/')}/api/offers",
            headers=headers,
            params=params,
            proxies=network_profile["proxies"],
            timeout=REQUEST_TIMEOUT,
            retries=REQUEST_RETRIES,
            backoff=2.0,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"GET /api/offers failed: {resp.status_code} {resp.text[:500]}")

        data = resp.json()
        offers = data.get("offers", [])
        total_count = int(data.get("total_count", 0))

        all_offers.extend(offers)

        if not offers or (offset + len(offers)) >= total_count:
            break
        offset += len(offers)

    return all_offers


def _lookup_supplier_skus(shop_skus: List[str]) -> Dict[str, str]:
    if not shop_skus:
        return {}
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            placeholders = ",".join(["%s"] * len(shop_skus))
            sql = f"""
                SELECT SKU, warehouse_SKU
                FROM autooperate.mapping_table
                WHERE SKU IN ({placeholders})
            """
            cursor.execute(sql, shop_skus)
            return {row["SKU"]: row["warehouse_SKU"] for row in cursor.fetchall()}
    finally:
        conn.close()


def _upsert_offers(platform: str, shop_name: str, records: List[Dict]) -> Tuple[int, int]:
    if not records:
        return 0, 0
    conn = DBManager.get_connection()
    try:
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        sql = """
            INSERT INTO order_system.offerprice_listing
                (platform, shop_name, shop_sku, sku, title, category, price, quantity, status, listed_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
                sku        = VALUES(sku),
                title      = VALUES(title),
                category   = VALUES(category),
                price      = VALUES(price),
                quantity   = VALUES(quantity),
                status     = VALUES(status),
                listed_at  = VALUES(listed_at),
                updated_at = VALUES(updated_at)
        """
        rows = [
            (
                platform,
                shop_name,
                r["shop_sku"],
                r.get("sku") or None,
                r.get("title") or None,
                r.get("category") or None,
                r.get("price"),
                r.get("quantity"),
                r.get("status", "ACTIVE"),
                r.get("listed_at"),
                now,
            )
            for r in records
        ]
        with conn.cursor() as cursor:
            cursor.executemany(sql, rows)
            affected = cursor.rowcount
        conn.commit()
    finally:
        conn.close()
    return len(records), affected


def run_offer_listing_sync(store_key: str) -> Dict:
    cfg = STORE_CONFIGS.get(store_key)
    if not cfg:
        return {"success": False, "msg": f"unknown store: {store_key}"}

    base_dir = current_app.config.get("BASE_DIR", ".")
    store_cfg = load_store_config(base_dir, store_key)
    api_key = store_cfg["api_key"]
    api_url = store_cfg["api_url"]

    if not api_key:
        return {"success": False, "msg": f"api_key missing for {store_key}"}

    network_profile = _load_network_profile(store_key)

    try:
        offers_raw = _fetch_all_offers(api_url, api_key, network_profile)
    except Exception as exc:
        return {"success": False, "msg": str(exc)}

    shop_skus = [_norm(o.get("shop_sku")) for o in offers_raw if _norm(o.get("shop_sku"))]
    sku_map = _lookup_supplier_skus(shop_skus)

    records = []
    for o in offers_raw:
        shop_sku = _norm(o.get("shop_sku"))
        if not shop_sku:
            continue

        active = o.get("active", True)
        status = "ACTIVE" if active else "INACTIVE"

        price = o.get("price")
        if price is not None:
            try:
                price = float(price)
            except (ValueError, TypeError):
                price = None

        quantity = o.get("quantity")
        if quantity is not None:
            try:
                quantity = int(quantity)
            except (ValueError, TypeError):
                quantity = None

        cat = o.get("category_label") or o.get("category_code") or ""
        if isinstance(cat, dict):
            cat = cat.get("label") or cat.get("code") or ""

        records.append({
            "shop_sku": shop_sku,
            "sku": sku_map.get(shop_sku) or None,
            "title": _norm(o.get("product_title")),
            "category": _norm(cat),
            "price": price,
            "quantity": quantity,
            "status": status,
            "listed_at": _parse_datetime(o.get("creation_date") or o.get("update_date")),
        })

    total, affected = _upsert_offers(cfg["platform"], cfg["shop_name"], records)

    return {
        "success": True,
        "store": store_key,
        "fetched": len(offers_raw),
        "upserted": total,
        "db_affected_rows": affected,
    }
