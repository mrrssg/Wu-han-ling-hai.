"""
Fetch package dimensions + weight from the Feishu bitable
HD-TOP-Mirkal (tblxHsORDrH6Ldvr) for HD Vevor shipping.

Credentials live in `instance/feishu_app.json`:
    {"app_id": "cli_xxx", "app_secret": "xxx"}

Schema we care about (field names exact-match):
    Shop SKU        text     -- key, e.g. "YCVE-WF982082"
    供应商SKU         text     -- warehouse SKU (mapping_table.warehouse_SKU)
    供应商            single   -- "Vevor" / "Costway"
    长in（包装）      number   -- length inches (raw, may be decimal)
    宽in（包装）      number   -- width
    高in（包装）      number   -- height
    重LB（包装）      number   -- weight pounds
"""
import os
import json
import math
import time
import logging
from typing import Any, Dict, Iterable, List, Optional

import requests
from flask import current_app


log = logging.getLogger(__name__)

APP_TOKEN = "QEeubiXYGa83zXs3Zt8cSSJPnih"
TABLE_ID = "tblxHsORDrH6Ldvr"
BASE = "https://open.feishu.cn/open-apis"

# In-process token cache (process-local; gunicorn worker keeps its own)
_TOKEN_CACHE: Dict[str, Any] = {"token": None, "expires_at": 0.0}


def _load_creds() -> Dict[str, str]:
    base = current_app.config["BASE_DIR"]
    path = os.path.join(base, "instance", "feishu_app.json")
    if not os.path.exists(path):
        raise RuntimeError(f"Feishu credentials missing: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    app_id = (data.get("app_id") or "").strip()
    app_secret = (data.get("app_secret") or "").strip()
    if not app_id or not app_secret:
        raise RuntimeError("instance/feishu_app.json missing app_id/app_secret")
    return {"app_id": app_id, "app_secret": app_secret}


def _get_token(force_refresh: bool = False) -> str:
    now = time.time()
    if not force_refresh and _TOKEN_CACHE["token"] and _TOKEN_CACHE["expires_at"] > now + 60:
        return _TOKEN_CACHE["token"]
    creds = _load_creds()
    r = requests.post(
        f"{BASE}/auth/v3/tenant_access_token/internal",
        json={"app_id": creds["app_id"], "app_secret": creds["app_secret"]},
        timeout=15,
    )
    r.raise_for_status()
    body = r.json()
    if body.get("code") != 0:
        raise RuntimeError(f"Feishu auth failed: {body}")
    token = body["tenant_access_token"]
    expire = int(body.get("expire") or 7200)
    _TOKEN_CACHE["token"] = token
    _TOKEN_CACHE["expires_at"] = now + expire
    return token


def _auth_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {_get_token()}",
        "Content-Type": "application/json; charset=utf-8",
    }


def _extract_cell(value: Any) -> Optional[str]:
    """Feishu text cells come back as list of {text,type}."""
    if value is None:
        return None
    if isinstance(value, list):
        if not value:
            return None
        first = value[0]
        if isinstance(first, dict):
            return first.get("text")
        return str(first)
    return value if isinstance(value, str) else str(value)


def _number(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_dims_for_shop_skus(shop_skus: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """Bulk-fetch dimensions for many Shop SKU values.

    Returns: { shop_sku -> {warehouse_sku, supplier, length_in, width_in,
                            depth_in, weight_lb, length_in_ceil, ..., record_id} }
    Missing SKUs are simply absent from the dict.
    """
    skus = [s for s in shop_skus if s]
    out: Dict[str, Dict[str, Any]] = {}
    if not skus:
        return out

    # Feishu's "is" operator on text fields only allows a single value, so we
    # express IN as OR-joined per-value conditions. Chunk size kept modest to
    # stay under the request body limits.
    CHUNK = 50
    field_names = [
        "Shop SKU", "供应商SKU", "供应商",
        "长in（包装）", "宽in（包装）", "高in（包装）", "重LB（包装）",
    ]
    for i in range(0, len(skus), CHUNK):
        chunk = skus[i:i + CHUNK]
        body = {
            "field_names": field_names,
            "filter": {
                "conjunction": "or",
                "conditions": [
                    {"field_name": "Shop SKU", "operator": "is", "value": [sku]}
                    for sku in chunk
                ],
            },
        }
        page_token: Optional[str] = None
        while True:
            params = {"page_size": 200}
            if page_token:
                params["page_token"] = page_token
            r = requests.post(
                f"{BASE}/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/records/search",
                headers=_auth_headers(),
                params=params,
                data=json.dumps(body).encode("utf-8"),
                timeout=30,
            )
            try:
                data = r.json()
            except ValueError:
                log.warning("Feishu non-JSON response: %s", r.text[:300])
                break
            if data.get("code") != 0:
                log.warning("Feishu search failed code=%s msg=%s", data.get("code"), data.get("msg"))
                break
            for item in (data.get("data") or {}).get("items") or []:
                fields = item.get("fields") or {}
                shop_sku = _extract_cell(fields.get("Shop SKU"))
                if not shop_sku:
                    continue
                length = _number(fields.get("长in（包装）"))
                width = _number(fields.get("宽in（包装）"))
                depth = _number(fields.get("高in（包装）"))
                weight = _number(fields.get("重LB（包装）"))
                out[shop_sku] = {
                    "record_id": item.get("record_id"),
                    "warehouse_sku": _extract_cell(fields.get("供应商SKU")),
                    "supplier": fields.get("供应商"),
                    "length_in": length,
                    "width_in": width,
                    "depth_in": depth,
                    "weight_lb": weight,
                    "length_in_ceil": int(math.ceil(length)) if length else None,
                    "width_in_ceil": int(math.ceil(width)) if width else None,
                    "depth_in_ceil": int(math.ceil(depth)) if depth else None,
                    "weight_lb_ceil": int(math.ceil(weight)) if weight else None,
                }
            if not (data.get("data") or {}).get("has_more"):
                break
            page_token = (data.get("data") or {}).get("page_token")
            if not page_token:
                break
    return out


def fetch_dims_for_shop_sku(shop_sku: str) -> Optional[Dict[str, Any]]:
    res = fetch_dims_for_shop_skus([shop_sku])
    return res.get(shop_sku)
