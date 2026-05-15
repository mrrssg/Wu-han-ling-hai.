"""
Thin wrapper around Teapplix REST API for the HD Vevor shipping page.

API key: instance/hd_api_token.txt  (header: APIToken: <token>)
Base URL: https://api.teapplix.com/api2/

Endpoints we use:
  GET  /OrderNotification?Shipped=0   -- list unshipped orders
  GET  /ShippingProfile               -- list configured warehouses
  GET  /ShipMethod                    -- list available carrier services
  POST /PurchaseLabelForOrder         -- buy UPS label, returns tracking + URL
  POST /CancelLabel                   -- cancel previously purchased label
"""
import os
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import current_app


log = logging.getLogger(__name__)

BASE_URL = "https://api.teapplix.com/api2"
DEFAULT_TIMEOUT = 60  # PurchaseLabel can take 20-30s

# Retry on transient 5xx / network errors. 1s + 3s back-off keeps the user
# wait under ~5s while surviving a typical Teapplix nginx blip.
RETRY_BACKOFF = (1.0, 3.0)
RETRY_STATUSES = {502, 503, 504}


def _load_token(base_dir: Optional[str] = None) -> str:
    token = os.environ.get("TEAPPLIX_API_TOKEN", "").strip()
    if token:
        return token
    base = base_dir or current_app.config["BASE_DIR"]
    path = os.path.join(base, "instance", "hd_api_token.txt")
    if not os.path.exists(path):
        raise RuntimeError(f"Teapplix API token not found at {path}")
    with open(path, "r", encoding="utf-8") as f:
        token = f.read().strip()
    if not token:
        raise RuntimeError("Teapplix API token file is empty")
    return token


def _headers() -> Dict[str, str]:
    return {
        "APIToken": _load_token(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _request_with_retry(method: str, path: str,
                        params: Optional[Dict[str, Any]] = None,
                        json_body: Optional[Dict[str, Any]] = None,
                        retry_on_post: bool = False) -> Dict[str, Any]:
    """GET is always retried on 5xx. POST is only retried if explicitly opted-in
    (label purchase is NOT retried — we don't want to double-charge postage)."""
    url = f"{BASE_URL}{path}"
    attempts = len(RETRY_BACKOFF) + 1
    last_resp = None
    for attempt in range(attempts):
        try:
            if method == "GET":
                r = requests.get(url, headers=_headers(), params=params,
                                 timeout=DEFAULT_TIMEOUT)
            else:
                r = requests.post(url, headers=_headers(),
                                  data=json.dumps(json_body or {}).encode("utf-8"),
                                  timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as e:
            log.warning("Teapplix %s %s network error attempt %d: %s",
                        method, path, attempt + 1, e)
            if attempt == attempts - 1:
                return {"status": 0, "body": {"raw": f"network error: {e}"}}
            time.sleep(RETRY_BACKOFF[attempt])
            continue

        try:
            body = r.json()
        except ValueError:
            body = {"raw": r.text}
        last_resp = {"status": r.status_code, "body": body}

        retriable = r.status_code in RETRY_STATUSES and (method == "GET" or retry_on_post)
        if not retriable or attempt == attempts - 1:
            if r.status_code >= 400:
                log.warning("Teapplix %s %s -> %s (attempt %d)",
                            method, path, r.status_code, attempt + 1)
            return last_resp
        log.info("Teapplix %s %s got %s, retrying after %.1fs",
                 method, path, r.status_code, RETRY_BACKOFF[attempt])
        time.sleep(RETRY_BACKOFF[attempt])
    return last_resp


def _get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return _request_with_retry("GET", path, params=params)


def _post(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    # POST is non-idempotent for PurchaseLabel (could double-charge) so we do
    # NOT retry by default. CancelLabel is idempotent so we could but err on
    # the side of caution and let the caller see the error.
    return _request_with_retry("POST", path, json_body=payload, retry_on_post=False)


# =============================================================================
# High-level helpers
# =============================================================================

def list_unshipped_orders() -> List[Dict[str, Any]]:
    """Pull all unshipped orders. Teapplix paginates implicitly; we keep
    the first page which is what the UI list endpoint exposes anyway."""
    res = _get("/OrderNotification", params={"Shipped": 0})
    if res["status"] != 200:
        raise RuntimeError(f"Teapplix OrderNotification failed: {res['body']}")
    return res["body"].get("Orders") or []


def get_order(txn_id: str) -> Optional[Dict[str, Any]]:
    res = _get("/OrderNotification", params={"TxnId": txn_id})
    if res["status"] != 200:
        return None
    orders = res["body"].get("Orders") or []
    return orders[0] if orders else None


def list_shipping_profiles() -> List[Dict[str, Any]]:
    res = _get("/ShippingProfile")
    if res["status"] != 200:
        raise RuntimeError(f"Teapplix ShippingProfile failed: {res['body']}")
    return res["body"].get("Profiles") or []


def list_ship_methods() -> List[Dict[str, Any]]:
    res = _get("/ShipMethod")
    if res["status"] != 200:
        raise RuntimeError(f"Teapplix ShipMethod failed: {res['body']}")
    return res["body"].get("Methods") or []


def purchase_label(
    txn_id: str,
    profile_id: int,
    method: str,
    weight_lb: float,
    length_in: float,
    width_in: float,
    depth_in: float,
    quantity: int = 1,
    line_number: int = 1,
) -> Dict[str, Any]:
    """POST /PurchaseLabelForOrder. Returns the raw response dict so the caller
    can inspect TrackingInfo / LabelData / PostageAmount / Status."""
    payload = {
        "TxnId": txn_id,
        "From": {"ProfileId": profile_id},
        "Packages": [{
            "Method": method,
            "Weight": {"Value": float(weight_lb), "Unit": "LB"},
            "Dimensions": {
                "Length": float(length_in),
                "Width": float(width_in),
                "Depth": float(depth_in),
                "Unit": "IN",
            },
            "PackageItems": [{
                "LineNumber": line_number,
                "Quantity": quantity,
            }],
        }],
        "ImageFormat": {"Type": "PDF", "LabelReturn": "URL"},
    }
    res = _post("/PurchaseLabelForOrder", payload)
    return {"request": payload, **res}


def download_label(label_url: str) -> Tuple[int, bytes, str]:
    """Fetch the PDF (or other format) at a Teapplix LabelData URL with the
    APIToken header. Returns (status_code, raw_bytes, content_type).
    The URL points to DownloadLabel/* which is gated by the API token."""
    if not label_url:
        return 400, b"missing label_url", "text/plain"
    headers = {"APIToken": _load_token()}
    r = requests.get(label_url, headers=headers, timeout=DEFAULT_TIMEOUT, stream=False)
    return r.status_code, r.content, r.headers.get("Content-Type", "application/pdf")


def cancel_label(txn_id: str, force: bool = False) -> Dict[str, Any]:
    """POST /CancelLabel by TxnId. force=True is needed for UPS/FedEx when
    the carrier cancellation errors out."""
    payload = {"TxnId": txn_id}
    if force:
        payload["Force"] = True
    res = _post("/CancelLabel", payload)
    return {"request": payload, **res}


# =============================================================================
# Response extraction helpers
# =============================================================================

def extract_tracking(response_body: Dict[str, Any]) -> Optional[str]:
    """First tracking number from a Teapplix Purchase response."""
    if not response_body:
        return None
    infos = response_body.get("TrackingInfo") or []
    if infos and isinstance(infos, list):
        first = infos[0] or {}
        return first.get("TrackingNumber")
    return None


def extract_label_url(response_body: Dict[str, Any]) -> Optional[str]:
    """First label URL from LabelData (when LabelReturn=URL was requested)."""
    if not response_body:
        return None
    items = response_body.get("LabelData") or []
    if items and isinstance(items, list):
        first = items[0] or {}
        return first.get("Content")
    return None


def extract_postage(response_body: Dict[str, Any]) -> Optional[float]:
    if not response_body:
        return None
    p = response_body.get("PostageAmount")
    if isinstance(p, dict):
        v = p.get("Value")
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    try:
        return float(p) if p is not None else None
    except (TypeError, ValueError):
        return None


def extract_error(response_body: Dict[str, Any]) -> Tuple[Optional[int], Optional[str]]:
    """Pull (code, message) from a Teapplix 4xx/5xx body."""
    if not response_body:
        return None, None
    code = response_body.get("Code")
    msg = response_body.get("Message") or ""
    descs = response_body.get("Description") or []
    if isinstance(descs, list) and descs:
        msg = (msg + " — " + " | ".join(str(d) for d in descs)).strip(" —")
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None
    return code, (msg or None)
