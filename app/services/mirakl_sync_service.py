import json
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from flask import current_app

from app.models.db_manager import DBManager
from app.services.mirakl_shipping_service import (
    _load_network_profile,
    _request_with_retry,
    load_store_config,
)


SYNC_STORE_CONFIGS: Dict[str, Dict[str, str]] = {
    "macy_kuyotq": {
        "label": "Macy-Kuyotq",
        "platform": "macys-kuyotq",
        "shop_name": "kuyotq",
        "target_table": "order_system.macy_order_data",
    },
    "macy_wopet": {
        "label": "Macy-Wopet",
        "platform": "macys-wopet",
        "shop_name": "wopet",
        "target_table": "order_system.macy_order_data",
    },
    "bestbuy_delphi": {
        "label": "Bestbuy-Delphi",
        "platform": "bestbuy-delphi",
        "shop_name": "delphi",
        "target_table": "order_system.bestbuy_order_data",
    },
    "lowes_autool": {
        "label": "Lowes-Autool",
        "platform": "lowes-autool",
        "shop_name": "autool",
        "target_table": "order_system.lowes_order_data",
    },
}

DEFAULT_MAX = 100
MAX_MIN = 1
MAX_MAX = 100

SYNC_COOLDOWN_SECONDS_BY_ACTION: Dict[str, int] = {
    "sync": 600,
    "preview": 60,
}

SYNC_LOOKBACK_MINUTES = 10
AUTO_FALLBACK_HOURS = 24
SYNC_JITTER_MIN_SECONDS = 10
SYNC_JITTER_MAX_SECONDS = 60

UTC = timezone.utc
ET = ZoneInfo("America/New_York")


def _norm(value: str) -> str:
    return str(value or "").strip().lower()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _to_iso_utc(dt_utc: Optional[datetime]) -> str:
    if not dt_utc:
        return ""
    return dt_utc.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_mysql_utc(dt_utc: Optional[datetime]) -> Optional[str]:
    if not dt_utc:
        return None
    return dt_utc.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _to_mysql_et(dt_et: Optional[datetime]) -> Optional[str]:
    if not dt_et:
        return None
    return dt_et.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S")


def _parse_utc_value(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    text = str(value).strip()
    if not text:
        return None

    formats = (
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
    )
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _parse_et_input_to_utc(value: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("missing start_time_et")

    formats = (
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    )
    for fmt in formats:
        try:
            parsed = datetime.strptime(raw, fmt)
            return parsed.replace(tzinfo=ET).astimezone(UTC)
        except ValueError:
            continue
    raise ValueError("invalid ET datetime format")


def _utc_to_et_text(dt_utc: Optional[datetime]) -> str:
    if not dt_utc:
        return ""
    return dt_utc.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S")


def get_sync_store_options() -> Dict[str, str]:
    return {k: v["label"] for k, v in SYNC_STORE_CONFIGS.items()}


def is_sync_store_supported(store_key: str) -> bool:
    return _norm(store_key) in SYNC_STORE_CONFIGS


def _validate_sync_store(store_key: str) -> str:
    key = _norm(store_key)
    if key not in SYNC_STORE_CONFIGS:
        raise ValueError(f"unsupported store: {store_key}")
    return key


def _load_shop_row(store_key: str) -> Dict[str, Any]:
    key = _validate_sync_store(store_key)
    cfg = SYNC_STORE_CONFIGS[key]
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            sql = """
                SELECT id, platform, shop_name, last_sync_time, is_active
                FROM order_system.shop_configs
                WHERE LOWER(TRIM(platform)) = %s
                  AND LOWER(TRIM(shop_name)) = %s
                LIMIT 2
            """
            cursor.execute(sql, (cfg["platform"], cfg["shop_name"]))
            rows = cursor.fetchall()
    finally:
        conn.close()

    if not rows:
        raise RuntimeError(f"shop_configs not found for {key}")
    if len(rows) > 1:
        raise RuntimeError(f"duplicate shop_configs rows for {key}")

    row = rows[0]
    if int(row.get("is_active") or 0) != 1:
        raise RuntimeError(f"shop_configs row inactive for {key}")
    return row


def _cooldown_seconds_for_action(action_key: str) -> int:
    return int(SYNC_COOLDOWN_SECONDS_BY_ACTION.get(action_key) or 600)


def _api_name_for_action(action_key: str) -> str:
    return f"orders_api_{action_key}"


def _ensure_store_lock_row(cursor, store_key: str, action_key: str) -> None:
    cursor.execute(
        """
        INSERT INTO order_system.api_call_lock (shop_key, api_name, cooldown_seconds)
        VALUES (%s, %s, %s)
        ON DUPLICATE KEY UPDATE shop_key = VALUES(shop_key)
        """,
        (store_key, _api_name_for_action(action_key), _cooldown_seconds_for_action(action_key)),
    )


def try_acquire_orders_api_cooldown(store_key: str, action: str) -> Dict[str, Any]:
    key = _validate_sync_store(store_key)
    action_key = _norm(action)
    if action_key not in {"sync", "preview"}:
        raise ValueError("action must be sync or preview")
    cooldown_seconds = _cooldown_seconds_for_action(action_key)
    api_name = _api_name_for_action(action_key)

    now_utc = _utc_now()
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            _ensure_store_lock_row(cursor, key, action_key)
            cursor.execute(
                """
                SELECT cooldown_seconds, last_action, last_called_at_utc, next_allowed_at_utc
                FROM order_system.api_call_lock
                WHERE shop_key = %s AND api_name = %s
                FOR UPDATE
                """,
                (key, api_name),
            )
            row = cursor.fetchone() or {}

            next_allowed = _parse_utc_value(row.get("next_allowed_at_utc"))

            if next_allowed and now_utc < next_allowed:
                remaining = int((next_allowed - now_utc).total_seconds())
                conn.rollback()
                return {
                    "ok": False,
                    "remaining_seconds": max(1, remaining),
                    "last_action": row.get("last_action") or "",
                    "next_allowed_at_utc": _to_iso_utc(next_allowed),
                }

            next_allowed_new = now_utc + timedelta(seconds=cooldown_seconds)
            cursor.execute(
                """
                UPDATE order_system.api_call_lock
                SET cooldown_seconds = %s,
                    last_action = %s,
                    last_called_at_utc = %s,
                    next_allowed_at_utc = %s,
                    lock_version = lock_version + 1
                WHERE shop_key = %s AND api_name = %s
                """,
                (
                    cooldown_seconds,
                    action_key,
                    _to_mysql_utc(now_utc),
                    _to_mysql_utc(next_allowed_new),
                    key,
                    api_name,
                ),
            )
        conn.commit()
        return {
            "ok": True,
            "remaining_seconds": 0,
            "last_action": action_key,
            "next_allowed_at_utc": _to_iso_utc(next_allowed_new),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_orders_api_cooldown_status(store_key: str, action: str = "sync") -> Dict[str, Any]:
    key = _validate_sync_store(store_key)
    action_key = _norm(action)
    if action_key not in {"sync", "preview"}:
        raise ValueError("action must be sync or preview")
    api_name = _api_name_for_action(action_key)
    now_utc = _utc_now()
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            _ensure_store_lock_row(cursor, key, action_key)
            cursor.execute(
                """
                SELECT cooldown_seconds, last_action, last_called_at_utc, next_allowed_at_utc
                FROM order_system.api_call_lock
                WHERE shop_key = %s AND api_name = %s
                LIMIT 1
                """,
                (key, api_name),
            )
            row = cursor.fetchone() or {}
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    next_allowed = _parse_utc_value(row.get("next_allowed_at_utc"))
    last_called = _parse_utc_value(row.get("last_called_at_utc"))
    remaining = 0
    if next_allowed and now_utc < next_allowed:
        remaining = int((next_allowed - now_utc).total_seconds())

    return {
        "cooldown_seconds": int(row.get("cooldown_seconds") or _cooldown_seconds_for_action(action_key)),
        "last_action": row.get("last_action") or "",
        "last_called_at_utc": _to_iso_utc(last_called),
        "next_allowed_at_utc": _to_iso_utc(next_allowed),
        "remaining_seconds": max(0, remaining),
    }


def _insert_log_row(
    store_key: str,
    platform: str,
    run_type: str,
    trigger_source: str,
    request_start_time_et: Optional[datetime],
    request_start_time_utc: Optional[datetime],
    checkpoint_before_utc: Optional[datetime],
    request_max: int,
    page_size: int,
) -> Tuple[int, datetime]:
    started_utc = _utc_now()
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO order_system.sync_job_logs (
                    shop_key, platform, run_type, trigger_source, status,
                    reason_code, reason_message,
                    request_start_time_et, request_start_time_utc, checkpoint_before_utc,
                    request_max, page_size, started_at_utc
                ) VALUES (%s, %s, %s, %s, 'running', NULL, NULL, %s, %s, %s, %s, %s, %s)
                """,
                (
                    store_key,
                    platform,
                    run_type,
                    trigger_source,
                    _to_mysql_et(request_start_time_et),
                    _to_mysql_utc(request_start_time_utc),
                    _to_mysql_utc(checkpoint_before_utc),
                    request_max,
                    page_size,
                    _to_mysql_utc(started_utc),
                ),
            )
            log_id = int(cursor.lastrowid)
        conn.commit()
        return log_id, started_utc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _finish_log_row(
    log_id: int,
    started_utc: datetime,
    status: str,
    reason_code: str = "",
    reason_message: str = "",
    checkpoint_after_utc: Optional[datetime] = None,
    pages_fetched: int = 0,
    orders_count: int = 0,
    order_lines_upserted: int = 0,
    api_http_status: Optional[int] = None,
) -> None:
    finished_utc = _utc_now()
    duration_ms = int(max(0, (finished_utc - started_utc).total_seconds() * 1000))
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE order_system.sync_job_logs
                SET status = %s,
                    reason_code = %s,
                    reason_message = %s,
                    checkpoint_after_utc = %s,
                    pages_fetched = %s,
                    orders_count = %s,
                    order_lines_upserted = %s,
                    api_http_status = %s,
                    finished_at_utc = %s,
                    duration_ms = %s
                WHERE id = %s
                """,
                (
                    status,
                    reason_code or None,
                    reason_message or None,
                    _to_mysql_utc(checkpoint_after_utc),
                    int(pages_fetched),
                    int(orders_count),
                    int(order_lines_upserted),
                    api_http_status,
                    _to_mysql_utc(finished_utc),
                    duration_ms,
                    log_id,
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _format_date_for_mysql(date_str: Optional[str]) -> Optional[str]:
    if not date_str:
        return None
    return str(date_str).replace("T", " ").replace("Z", "")


def _upsert_order_lines(
    cursor,
    target_table: str,
    shop_id: int,
    platform: str,
    orders: List[Dict[str, Any]],
) -> int:
    allowed_tables = {
        "order_system.macy_order_data",
        "order_system.bestbuy_order_data",
        "order_system.lowes_order_data",
    }
    if target_table not in allowed_tables:
        raise RuntimeError(f"unsupported target table: {target_table}")

    count = 0
    for order in orders:
        order_id = order.get("order_id")
        comm_id = order.get("commercial_id")
        c_date = _format_date_for_mysql(order.get("created_date"))
        a_date = _format_date_for_mysql(order.get("acceptance_decision_date"))
        s_deadline = _format_date_for_mysql(order.get("shipping_deadline"))
        u_date = _format_date_for_mysql(order.get("last_updated_date"))

        o_state = order.get("order_state")
        total_order_amt = order.get("total_price")
        currency = order.get("currency_iso_code")

        cust_obj = order.get("customer") or {}
        ship_addr = cust_obj.get("shipping_address") or {}
        cust_id = cust_obj.get("customer_id")
        city = ship_addr.get("city")
        state = ship_addr.get("state")
        zip_code = ship_addr.get("zip_code")
        email = order.get("customer_notification_email")

        s_type = order.get("shipping_type_label")
        s_company = order.get("shipping_company")
        s_tracking = order.get("shipping_tracking")
        leadtime = order.get("leadtime_to_ship")

        for line in (order.get("order_lines") or []):
            ol_id = line.get("order_line_id")
            sku = line.get("offer_sku")
            title = line.get("product_title")
            off_id = line.get("offer_id")
            p_sku = line.get("product_sku")
            cat_label = line.get("category_label")
            cat_code = line.get("category_code")

            p_unit = line.get("price_unit", 0.0)
            qty = line.get("quantity", 0)
            l_total = line.get("total_price", 0.0)
            l_shipping = line.get("shipping_price") or 0.0
            l_comm = line.get("commission_fee", 0.0)

            taxes = line.get("taxes") or []
            tax_amt = taxes[0].get("amount") if (taxes and isinstance(taxes[0], dict)) else 0.0

            promos = line.get("promotions") or []
            camp_id, promo_type, perc_off, ded_amt = None, None, 0.0, 0.0
            if promos and isinstance(promos[0], dict):
                promo = promos[0]
                camp_id = (promo.get("campaign") or {}).get("identifier")
                promo_type = (promo.get("configuration") or {}).get("type")
                perc_off = (promo.get("configuration") or {}).get("percentage_off") or 0.0
                ded_amt = promo.get("deduced_amount") or 0.0

            sql = f"""
            INSERT INTO {target_table} (
                shop_id, platform, order_id, commercial_id, order_line_id,
                created_date, acceptance_decision_date, shipping_deadline, last_updated_date,
                offer_sku, product_title, offer_id, product_sku, category_label, category_code,
                price_unit, quantity, line_total_price, shipping_price, commission_fee, tax_amount,
                currency, total_order_amount, campaign_id, promotion_type, percentage_off, deduced_amount,
                customer_id, shipping_city, shipping_state, shipping_zip, customer_email,
                order_state, shipping_type_label, shipping_company, shipping_tracking, leadtime_to_ship,
                raw_json
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                order_state = VALUES(order_state),
                last_updated_date = VALUES(last_updated_date),
                shipping_tracking = VALUES(shipping_tracking),
                raw_json = VALUES(raw_json)
            """

            values = (
                shop_id,
                platform,
                order_id,
                comm_id,
                ol_id,
                c_date,
                a_date,
                s_deadline,
                u_date,
                sku,
                title,
                off_id,
                p_sku,
                cat_label,
                cat_code,
                p_unit,
                qty,
                l_total,
                l_shipping,
                l_comm,
                tax_amt,
                currency,
                total_order_amt,
                camp_id,
                promo_type,
                perc_off,
                ded_amt,
                cust_id,
                city,
                state,
                zip_code,
                email,
                o_state,
                s_type,
                s_company,
                s_tracking,
                leadtime,
                json.dumps(order, ensure_ascii=False),
            )
            cursor.execute(sql, values)
            count += 1
    return count


def _build_query_start_utc(
    run_type: str,
    manual_start_time_et: str,
    checkpoint_before_utc: Optional[datetime],
    request_started_utc: datetime,
) -> Tuple[Optional[datetime], datetime]:
    random_seconds = random.randint(1, 30)
    if run_type == "manual":
        selected_utc = _parse_et_input_to_utc(manual_start_time_et)
        query_start_utc = selected_utc - timedelta(seconds=random_seconds)
        return selected_utc.astimezone(ET), query_start_utc

    if checkpoint_before_utc:
        query_start_utc = checkpoint_before_utc - timedelta(
            minutes=SYNC_LOOKBACK_MINUTES,
            seconds=random_seconds,
        )
        return None, query_start_utc

    fallback_start = request_started_utc - timedelta(hours=AUTO_FALLBACK_HOURS, seconds=random_seconds)
    return None, fallback_start


def run_order_sync_job(
    store_key: str,
    run_type: str = "auto",
    trigger_source: str = "cron",
    manual_start_time_et: str = "",
    max_value: int = DEFAULT_MAX,
) -> Dict[str, Any]:
    key = _validate_sync_store(store_key)
    store_cfg = SYNC_STORE_CONFIGS[key]

    run_type = _norm(run_type)
    if run_type not in {"auto", "manual"}:
        raise ValueError("run_type must be auto or manual")

    trigger_source = _norm(trigger_source)
    if trigger_source not in {"cron", "ui"}:
        raise ValueError("trigger_source must be cron or ui")

    page_size = max(MAX_MIN, min(int(max_value or DEFAULT_MAX), MAX_MAX))
    shop_row = _load_shop_row(key)
    checkpoint_before_utc = _parse_utc_value(shop_row.get("last_sync_time"))
    request_started_utc = _utc_now()

    request_start_et, query_start_utc = _build_query_start_utc(
        run_type=run_type,
        manual_start_time_et=manual_start_time_et,
        checkpoint_before_utc=checkpoint_before_utc,
        request_started_utc=request_started_utc,
    )

    log_id, log_started_utc = _insert_log_row(
        store_key=key,
        platform=store_cfg["platform"],
        run_type=run_type,
        trigger_source=trigger_source,
        request_start_time_et=request_start_et,
        request_start_time_utc=query_start_utc,
        checkpoint_before_utc=checkpoint_before_utc,
        request_max=page_size,
        page_size=page_size,
    )

    cooldown = try_acquire_orders_api_cooldown(key, action="sync")
    if not cooldown.get("ok"):
        msg = (
            f"/api/orders cooldown active, wait {cooldown.get('remaining_seconds', 0)}s "
            f"(last_action={cooldown.get('last_action', '')})"
        )
        _finish_log_row(
            log_id=log_id,
            started_utc=log_started_utc,
            status="skipped",
            reason_code="cooldown_block",
            reason_message=msg,
        )
        return {
            "success": False,
            "status": "skipped",
            "store_key": key,
            "msg": msg,
            "remaining_seconds": cooldown.get("remaining_seconds", 0),
            "log_id": log_id,
        }

    base_dir = current_app.config.get("BASE_DIR", current_app.root_path)
    api_cfg = load_store_config(base_dir, key)
    api_key = str(api_cfg.get("api_key") or "").strip()
    api_url = str(api_cfg.get("api_url") or "").strip()
    if not api_key:
        _finish_log_row(
            log_id=log_id,
            started_utc=log_started_utc,
            status="failed",
            reason_code="missing_api_key",
            reason_message=f"missing api key: instance/{key}_key.txt",
        )
        return {"success": False, "status": "failed", "store_key": key, "msg": "missing api key", "log_id": log_id}

    network_profile = _load_network_profile(key)
    headers = {
        "Authorization": api_key,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "close",
        "User-Agent": network_profile["user_agent"],
    }

    offset = 0
    pages_fetched = 0
    orders_count = 0
    lines_upserted = 0
    api_http_status = None
    success = False
    error_code = ""
    error_message = ""

    conn = DBManager.get_connection()
    try:
        while True:
            jitter = random.randint(SYNC_JITTER_MIN_SECONDS, SYNC_JITTER_MAX_SECONDS)
            time.sleep(jitter)

            params = {
                "start_update_date": _to_iso_utc(query_start_utc),
                "max": page_size,
                "offset": offset,
                "paginate": "true",
            }

            resp = _request_with_retry(
                method="GET",
                url=f"{api_url.rstrip('/')}/api/orders",
                headers=headers,
                params=params,
                proxies=network_profile["proxies"],
                timeout=60,
            )
            api_http_status = resp.status_code
            if resp.status_code != 200:
                error_code = "api_error"
                error_message = f"mirakl request failed: {resp.status_code} {resp.text}"
                break

            try:
                payload = resp.json()
            except Exception as exc:
                error_code = "json_error"
                error_message = f"invalid json response: {exc}"
                break

            orders = payload.get("orders", []) or []
            total_count = int(payload.get("total_count") or 0)
            pages_fetched += 1

            if orders:
                with conn.cursor() as cursor:
                    lines = _upsert_order_lines(
                        cursor=cursor,
                        target_table=store_cfg["target_table"],
                        shop_id=int(shop_row["id"]),
                        platform=str(shop_row.get("platform") or ""),
                        orders=orders,
                    )
                conn.commit()
                lines_upserted += lines
                orders_count += len(orders)

            if offset + len(orders) >= total_count:
                success = True
                break

            if not orders and total_count > offset:
                error_code = "pagination_error"
                error_message = "empty page while total_count has remaining records"
                break

            offset += page_size
    except Exception as exc:
        conn.rollback()
        error_code = "exception"
        error_message = str(exc)
    finally:
        conn.close()

    if not success:
        _finish_log_row(
            log_id=log_id,
            started_utc=log_started_utc,
            status="failed",
            reason_code=error_code or "unknown_error",
            reason_message=error_message or "sync failed",
            pages_fetched=pages_fetched,
            orders_count=orders_count,
            order_lines_upserted=lines_upserted,
            api_http_status=api_http_status,
        )
        return {
            "success": False,
            "status": "failed",
            "store_key": key,
            "msg": error_message or "sync failed",
            "log_id": log_id,
            "pages_fetched": pages_fetched,
            "orders_count": orders_count,
            "order_lines_upserted": lines_upserted,
        }

    checkpoint_after_utc = request_started_utc
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE order_system.shop_configs SET last_sync_time = %s WHERE id = %s",
                (_to_iso_utc(checkpoint_after_utc), int(shop_row["id"])),
            )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        _finish_log_row(
            log_id=log_id,
            started_utc=log_started_utc,
            status="failed",
            reason_code="checkpoint_update_error",
            reason_message=str(exc),
            pages_fetched=pages_fetched,
            orders_count=orders_count,
            order_lines_upserted=lines_upserted,
            api_http_status=api_http_status,
        )
        return {
            "success": False,
            "status": "failed",
            "store_key": key,
            "msg": f"checkpoint update failed: {exc}",
            "log_id": log_id,
        }
    finally:
        conn.close()

    _finish_log_row(
        log_id=log_id,
        started_utc=log_started_utc,
        status="success",
        checkpoint_after_utc=checkpoint_after_utc,
        pages_fetched=pages_fetched,
        orders_count=orders_count,
        order_lines_upserted=lines_upserted,
        api_http_status=api_http_status,
    )
    return {
        "success": True,
        "status": "success",
        "store_key": key,
        "msg": "sync completed",
        "log_id": log_id,
        "pages_fetched": pages_fetched,
        "orders_count": orders_count,
        "order_lines_upserted": lines_upserted,
        "checkpoint_after_utc": _to_iso_utc(checkpoint_after_utc),
    }


def get_sync_dashboard(store_key: str, log_limit: int = 20) -> Dict[str, Any]:
    key = _validate_sync_store(store_key)
    shop_row = _load_shop_row(key)
    last_sync_utc = _parse_utc_value(shop_row.get("last_sync_time"))
    cooldown = get_orders_api_cooldown_status(key)

    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, run_type, trigger_source, status,
                       reason_code, reason_message,
                       request_start_time_et, request_start_time_utc,
                       checkpoint_before_utc, checkpoint_after_utc,
                       request_max, page_size, pages_fetched, orders_count, order_lines_upserted,
                       api_http_status, started_at_utc, finished_at_utc, duration_ms
                FROM order_system.sync_job_logs
                WHERE shop_key = %s
                ORDER BY id DESC
                LIMIT %s
                """,
                (key, int(max(1, log_limit))),
            )
            rows = cursor.fetchall()
    finally:
        conn.close()

    logs: List[Dict[str, Any]] = []
    for row in rows:
        started_utc = _parse_utc_value(row.get("started_at_utc"))
        finished_utc = _parse_utc_value(row.get("finished_at_utc"))
        req_start_utc = _parse_utc_value(row.get("request_start_time_utc"))
        checkpoint_before_utc = _parse_utc_value(row.get("checkpoint_before_utc"))
        checkpoint_after_utc = _parse_utc_value(row.get("checkpoint_after_utc"))

        req_start_et_raw = row.get("request_start_time_et")
        req_start_et_text = (
            req_start_et_raw.strftime("%Y-%m-%d %H:%M:%S")
            if isinstance(req_start_et_raw, datetime)
            else str(req_start_et_raw or "")
        )

        logs.append(
            {
                "id": row.get("id"),
                "run_type": row.get("run_type") or "",
                "trigger_source": row.get("trigger_source") or "",
                "status": row.get("status") or "",
                "reason_code": row.get("reason_code") or "",
                "reason_message": row.get("reason_message") or "",
                "request_max": int(row.get("request_max") or 0),
                "page_size": int(row.get("page_size") or 0),
                "pages_fetched": int(row.get("pages_fetched") or 0),
                "orders_count": int(row.get("orders_count") or 0),
                "order_lines_upserted": int(row.get("order_lines_upserted") or 0),
                "api_http_status": row.get("api_http_status"),
                "duration_ms": int(row.get("duration_ms") or 0),
                "started_at_utc": _to_iso_utc(started_utc),
                "finished_at_utc": _to_iso_utc(finished_utc),
                "request_start_time_utc": _to_iso_utc(req_start_utc),
                "checkpoint_before_utc": _to_iso_utc(checkpoint_before_utc),
                "checkpoint_after_utc": _to_iso_utc(checkpoint_after_utc),
                "started_at_et": _utc_to_et_text(started_utc),
                "finished_at_et": _utc_to_et_text(finished_utc),
                "request_start_time_et": req_start_et_text,
                "query_start_time_et": _utc_to_et_text(req_start_utc),
                "checkpoint_before_et": _utc_to_et_text(checkpoint_before_utc),
                "checkpoint_after_et": _utc_to_et_text(checkpoint_after_utc),
            }
        )

    return {
        "store_key": key,
        "store_label": SYNC_STORE_CONFIGS[key]["label"],
        "stores": get_sync_store_options(),
        "last_sync_time_utc": _to_iso_utc(last_sync_utc),
        "last_sync_time_et": _utc_to_et_text(last_sync_utc),
        "cooldown": cooldown,
        "logs": logs,
    }
