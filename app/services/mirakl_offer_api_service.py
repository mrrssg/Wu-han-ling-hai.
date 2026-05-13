"""
Mirakl Offer API helpers - OF21/OF22/OF24/OF52/OF53.

Key invariants:
1. ALL Mirakl HTTP calls go through the store's dedicated proxy IP via
   `mirakl_shipping_service._load_network_profile(store_key)` so they remain
   IP-isolated. There is no other way to call Mirakl from autoweb.
2. ALL state-changing or pricing-related Mirakl calls (OF24/OF52/OF53)
   acquire the `offers_api` cooldown row in `order_system.api_call_lock` so
   the project-wide rate limiter applies. The Mirakl docs cap OF24 at 1/min;
   we set cooldown to 65 s to keep a safety buffer.
3. Numeric money fields written to Mirakl are `round(v, 2)` -
   see feedback_mirakl_number_precision in memory.

Designed only for macy_kuyotq for now; STORE_KEYS gate keeps other stores
out until they have been deliberately enabled.
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

import requests
from flask import current_app

from app.models.db_manager import DBManager
from app.services.mirakl_shipping_service import (
    _load_network_profile,
    _request_with_retry,
    load_store_config,
)


SUPPORTED_STORE_KEYS = {"macy_kuyotq"}

# Cooldown configuration. Mirakl OF24 hard cap = 1/min. 65s buffer.
COOLDOWN_API_NAME = "offers_api"
COOLDOWN_SECONDS = 65

# OF52/OF53 polling defaults.
OF52_DEFAULT_EXPORT_TYPE = "application/json"
OF53_POLL_INTERVAL_SECONDS = 60
OF53_MAX_POLLS = 60  # at most 60 minutes

# OF24 batch size start point (per design v3); raise/lower based on Mirakl behaviour.
OF24_DEFAULT_BATCH_SIZE = 50

REQUEST_TIMEOUT = 60
REQUEST_RETRIES = 2


# =============================================================================
# Cooldown lock (shared by all offer-API calls)
# =============================================================================

def _ensure_lock_row(cursor, store_key: str):
    cursor.execute(
        """INSERT INTO order_system.api_call_lock (shop_key, api_name, cooldown_seconds)
           VALUES (%s, %s, %s)
           ON DUPLICATE KEY UPDATE shop_key = VALUES(shop_key)""",
        (store_key, COOLDOWN_API_NAME, COOLDOWN_SECONDS),
    )


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_utc(val) -> Optional[datetime]:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.replace(tzinfo=timezone.utc) if val.tzinfo is None else val.astimezone(timezone.utc)
    text = str(val).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def acquire_offers_cooldown(store_key: str, action: str = "call") -> Dict[str, Any]:
    """Block (with FOR UPDATE) until the cooldown gate opens; bump the gate
    forward COOLDOWN_SECONDS, mark `last_action`, and return success.

    Returns:
        {"ok": True, "waited_seconds": float, "next_allowed_at_utc": str}
    Or raises if the lock row cannot be acquired.

    Behaviour: if the gate is in the future, we sleep until it opens. We do
    NOT return ok=False - this is a *waiting* lock not a *try* lock. Use
    `peek_offers_cooldown_remaining` for a non-blocking status check.
    """
    if store_key not in SUPPORTED_STORE_KEYS:
        raise ValueError(f"store_key not enabled: {store_key}")

    waited = 0.0
    while True:
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cursor:
                _ensure_lock_row(cursor, store_key)
                cursor.execute(
                    """SELECT next_allowed_at_utc
                         FROM order_system.api_call_lock
                        WHERE shop_key=%s AND api_name=%s
                        FOR UPDATE""",
                    (store_key, COOLDOWN_API_NAME),
                )
                row = cursor.fetchone() or {}
                next_allowed = _parse_utc(row.get("next_allowed_at_utc"))
                now = _now_utc()
                if next_allowed and now < next_allowed:
                    sleep_seconds = (next_allowed - now).total_seconds()
                    # release the lock row before sleeping so we do not block others
                    conn.commit()
                    time.sleep(min(sleep_seconds, 5.0))
                    waited += min(sleep_seconds, 5.0)
                    continue

                next_allowed_new = now + timedelta(seconds=COOLDOWN_SECONDS)
                cursor.execute(
                    """UPDATE order_system.api_call_lock
                          SET cooldown_seconds = %s,
                              last_action = %s,
                              last_called_at_utc = %s,
                              next_allowed_at_utc = %s,
                              lock_version = lock_version + 1
                        WHERE shop_key=%s AND api_name=%s""",
                    (
                        COOLDOWN_SECONDS,
                        action,
                        now.strftime("%Y-%m-%d %H:%M:%S"),
                        next_allowed_new.strftime("%Y-%m-%d %H:%M:%S"),
                        store_key,
                        COOLDOWN_API_NAME,
                    ),
                )
            conn.commit()
            return {
                "ok": True,
                "waited_seconds": round(waited, 1),
                "next_allowed_at_utc": next_allowed_new.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def peek_offers_cooldown_remaining(store_key: str) -> int:
    """Non-blocking. Return seconds until next allowed call, or 0 if already
    allowed. Used by web routes to surface the lock state.
    """
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            _ensure_lock_row(cursor, store_key)
            cursor.execute(
                """SELECT next_allowed_at_utc FROM order_system.api_call_lock
                    WHERE shop_key=%s AND api_name=%s LIMIT 1""",
                (store_key, COOLDOWN_API_NAME),
            )
            row = cursor.fetchone() or {}
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    next_allowed = _parse_utc(row.get("next_allowed_at_utc"))
    if not next_allowed:
        return 0
    return max(0, int((next_allowed - _now_utc()).total_seconds()))


# =============================================================================
# Low-level HTTP wrapper - always via the store's proxy
# =============================================================================

def _proxy_session_headers(store_key: str, api_key: str) -> Dict[str, str]:
    network_profile = _load_network_profile(store_key)
    return {
        "headers": {
            "Authorization": api_key,
            "Accept": "application/json",
            "User-Agent": network_profile["user_agent"],
            "Connection": "close",
        },
        "proxies": network_profile["proxies"],
        "ip_used": f"{network_profile['proxy_ip']}:{network_profile['proxy_port']}",
    }


def _resolve_api(store_key: str) -> Dict[str, str]:
    base_dir = current_app.config.get("BASE_DIR", current_app.root_path)
    cfg = load_store_config(base_dir, store_key)
    api_key = (cfg.get("api_key") or "").strip()
    api_url = (cfg.get("api_url") or "").strip()
    if not api_key:
        raise RuntimeError(f"missing api key for {store_key}")
    if not api_url:
        raise RuntimeError(f"missing api url for {store_key}")
    return {"api_key": api_key, "api_url": api_url.rstrip("/")}


# =============================================================================
# OF52 - submit async export
# =============================================================================

def submit_offer_export(
    store_key: str,
    *,
    last_request_date: Optional[str] = None,
    include_inactive: bool = False,
) -> Dict[str, Any]:
    """Submit an OF52 export. Returns {"tracking_id": ...}.

    If `last_request_date` is None, runs a full export (active offers only by
    default; pass include_inactive=True to also get inactive offers).
    """
    if store_key not in SUPPORTED_STORE_KEYS:
        raise ValueError(f"store_key not enabled: {store_key}")
    api = _resolve_api(store_key)
    net = _proxy_session_headers(store_key, api["api_key"])

    body: Dict[str, Any] = {
        "export_type": OF52_DEFAULT_EXPORT_TYPE,
    }
    if last_request_date:
        body["last_request_date"] = last_request_date
    else:
        # full mode
        if include_inactive:
            body["include_inactive_offers"] = True

    acquire_offers_cooldown(store_key, action="of52_submit")

    resp = _request_with_retry(
        method="POST",
        url=f"{api['api_url']}/api/offers/export/async",
        headers=net["headers"],
        json=body,
        proxies=net["proxies"],
        timeout=REQUEST_TIMEOUT,
        retries=REQUEST_RETRIES,
        backoff=2.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"OF52 submit failed: {resp.status_code} {resp.text[:500]}")
    data = resp.json()
    tracking_id = data.get("tracking_id")
    if not tracking_id:
        raise RuntimeError(f"OF52 submit returned no tracking_id: {data}")
    return {
        "tracking_id": tracking_id,
        "ip_used": net["ip_used"],
        "request_body": body,
    }


# =============================================================================
# OF53 - poll status, return urls when COMPLETED
# =============================================================================

def poll_offer_export(
    store_key: str,
    tracking_id: str,
    *,
    max_polls: int = OF53_MAX_POLLS,
    interval_seconds: int = OF53_POLL_INTERVAL_SECONDS,
) -> Dict[str, Any]:
    """Poll until status is COMPLETED or FAILED. Returns the final OF53 payload
    augmented with `polls_performed`.

    Sleeps `interval_seconds` between polls. Honours the cooldown lock on each
    poll so two concurrent monitors do not over-hammer Mirakl.
    """
    if store_key not in SUPPORTED_STORE_KEYS:
        raise ValueError(f"store_key not enabled: {store_key}")
    api = _resolve_api(store_key)
    net = _proxy_session_headers(store_key, api["api_key"])

    for poll_idx in range(1, max_polls + 1):
        acquire_offers_cooldown(store_key, action="of53_poll")
        resp = _request_with_retry(
            method="GET",
            url=f"{api['api_url']}/api/offers/export/async/status/{tracking_id}",
            headers=net["headers"],
            proxies=net["proxies"],
            timeout=REQUEST_TIMEOUT,
            retries=REQUEST_RETRIES,
            backoff=2.0,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"OF53 failed (poll {poll_idx}): {resp.status_code} {resp.text[:500]}")
        payload = resp.json()
        status = (payload.get("status") or "").upper()
        if status == "COMPLETED":
            return {
                **payload,
                "polls_performed": poll_idx,
                "ip_used": net["ip_used"],
            }
        if status == "FAILED":
            raise RuntimeError(f"OF52 export FAILED: {payload.get('error')}")
        # PENDING / unknown -> sleep before next poll (the cooldown already takes care of pacing)
        # but if cooldown was shorter than interval_seconds we still want the configured gap.
        # In practice cooldown==65 == interval_seconds==60 plus 5s buffer, so no extra sleep needed.

    raise RuntimeError(
        f"OF53 poll exceeded {max_polls} attempts for tracking_id={tracking_id}"
    )


# =============================================================================
# Chunk download (URLs are signed by Mirakl)
# =============================================================================

def download_export_chunks(
    urls: Iterable[str],
    store_key: str,
) -> List[Any]:
    """Download every chunk URL and concatenate the JSON `offers` arrays.

    OF52 chunks are gated on the same Mirakl Authorization header as the
    rest of the API. We send through the store proxy so the source IP stays
    consistent.
    """
    api = _resolve_api(store_key)
    net = _proxy_session_headers(store_key, api["api_key"])
    all_offers: List[Any] = []
    for url in urls:
        resp = requests.get(
            url,
            headers={
                "Authorization": api["api_key"],
                "Accept": "application/json",
                "User-Agent": net["headers"]["User-Agent"],
                "Connection": "close",
            },
            proxies=net["proxies"],
            timeout=120,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"chunk download failed: {resp.status_code} {resp.text[:500]}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise RuntimeError(f"chunk not JSON: {exc}") from exc
        # Mirakl OF52 chunks can return either {"offers": [...]} or a top-level array.
        if isinstance(payload, list):
            offers = payload
        elif isinstance(payload, dict):
            offers = payload.get("offers")
        else:
            offers = None
        if not isinstance(offers, list):
            keys_preview = list(payload.keys())[:5] if isinstance(payload, dict) else type(payload).__name__
            raise RuntimeError(f"chunk has no offers array: {keys_preview}")
        all_offers.extend(offers)
    return all_offers


# =============================================================================
# OF24 - update offers (writes; gated by dry_run)
# =============================================================================

def update_offers(
    store_key: str,
    payload_offers: List[Dict[str, Any]],
    *,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Send a batch of `update_delete='update'` offers via OF24.

    When `dry_run=True` (default), we DO NOT contact Mirakl. We compute the
    payload, return it for inspection, and the caller logs status=dry_run.
    Production callers must pass dry_run=False explicitly.
    """
    if store_key not in SUPPORTED_STORE_KEYS:
        raise ValueError(f"store_key not enabled: {store_key}")
    if not payload_offers:
        return {"sent": 0, "dry_run": dry_run, "import_id": None}
    if len(payload_offers) > OF24_DEFAULT_BATCH_SIZE * 2:
        raise ValueError(
            f"batch too large for safety: {len(payload_offers)} > "
            f"{OF24_DEFAULT_BATCH_SIZE * 2}; chunk before calling"
        )

    body = {"offers": payload_offers}

    if dry_run:
        return {
            "sent": len(payload_offers),
            "dry_run": True,
            "import_id": None,
            "request_body": body,
        }

    api = _resolve_api(store_key)
    net = _proxy_session_headers(store_key, api["api_key"])
    headers = {**net["headers"], "Content-Type": "application/json"}

    acquire_offers_cooldown(store_key, action="of24_update")

    resp = _request_with_retry(
        method="POST",
        url=f"{api['api_url']}/api/offers",
        headers=headers,
        json=body,
        proxies=net["proxies"],
        timeout=REQUEST_TIMEOUT,
        retries=REQUEST_RETRIES,
        backoff=2.0,
    )
    response_text = resp.text[:2000]
    if resp.status_code not in (200, 201):
        return {
            "sent": len(payload_offers),
            "dry_run": False,
            "http_status": resp.status_code,
            "response_body": response_text,
            "import_id": None,
            "ip_used": net["ip_used"],
            "error": f"OF24 returned {resp.status_code}",
        }
    try:
        data = resp.json()
    except ValueError:
        data = {}
    return {
        "sent": len(payload_offers),
        "dry_run": False,
        "http_status": resp.status_code,
        "import_id": data.get("import_id"),
        "response_body": response_text,
        "ip_used": net["ip_used"],
    }


# =============================================================================
# OF22 - get one offer (kept for debugging; not used in normal flow)
# =============================================================================

def get_offer(store_key: str, offer_id: int) -> Dict[str, Any]:
    if store_key not in SUPPORTED_STORE_KEYS:
        raise ValueError(f"store_key not enabled: {store_key}")
    api = _resolve_api(store_key)
    net = _proxy_session_headers(store_key, api["api_key"])
    acquire_offers_cooldown(store_key, action="of22_get")
    resp = _request_with_retry(
        method="GET",
        url=f"{api['api_url']}/api/offers/{offer_id}",
        headers=net["headers"],
        proxies=net["proxies"],
        timeout=REQUEST_TIMEOUT,
        retries=REQUEST_RETRIES,
        backoff=2.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"OF22 failed: {resp.status_code} {resp.text[:500]}")
    return resp.json()
