"""
One-shot probe: does OF21 GET /api/offers accept multiple SKUs in the `sku`
query parameter (comma-separated)? The official doc declares it as
`(string)` but Mirakl docs are sometimes loose about CSV-vs-single.

Runs 3 read-only probes through the macy_kuyotq proxy. Each probe consumes
one cooldown slot (~65s), so total wall time ~3 minutes.

Usage:
    PYTHONPATH=/var/www/autoweb/AutoWeb FLASK_CONFIG=production \
        ./venv/bin/python scripts/probe_of21_sku_csv.py
"""
import json
import os
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app import create_app
from app.services.mirakl_offer_api_service import (
    _proxy_session_headers,
    _resolve_api,
    acquire_offers_cooldown,
)
from app.services.mirakl_shipping_service import _request_with_retry


STORE = "macy_kuyotq"


def _call(label: str, params: dict) -> dict:
    print(f"\n=== Probe: {label} ===")
    print(f"  params: {params}")
    api = _resolve_api(STORE)
    net = _proxy_session_headers(STORE, api["api_key"])
    acquire_offers_cooldown(STORE, action=f"probe_{label}")
    t0 = time.time()
    resp = _request_with_retry(
        method="GET",
        url=f"{api['api_url']}/api/offers",
        headers=net["headers"],
        params=params,
        proxies=net["proxies"],
        timeout=60,
    )
    dt = time.time() - t0
    print(f"  HTTP {resp.status_code} ({dt:.1f}s)")
    if resp.status_code != 200:
        print(f"  body[:500]: {resp.text[:500]}")
        return {"status": resp.status_code, "body": resp.text[:500]}
    data = resp.json()
    offers = data.get("offers", [])
    total = data.get("total_count")
    skus = [o.get("shop_sku") for o in offers]
    print(f"  total_count: {total}")
    print(f"  offers returned: {len(offers)}")
    print(f"  shop_skus in result: {skus[:10]}{'...' if len(skus) > 10 else ''}")
    return {"status": 200, "total_count": total, "n": len(offers), "skus": skus}


def main() -> int:
    config_name = os.environ.get("FLASK_CONFIG", "production")
    app = create_app(config_name)

    with app.app_context():
        # Probe 1: single SKU (control)
        r1 = _call("01_single_sku", {"sku": "MRMC575443"})

        # Probe 2: two SKUs comma-separated
        r2 = _call("02_csv_two_skus",
                   {"sku": "MRMC575443,MRMC101245"})

        # Probe 3: 5 SKUs comma-separated
        r3 = _call("03_csv_five_skus",
                   {"sku": "MRMC575443,MRMC101245,MRMC189001,MRMC376001,MDMC044459"})

    print("\n\n=== Verdict ===")
    if r1.get("n") == 1 and r2.get("n") == 2 and r3.get("n") == 5:
        print("✅ sku param ACCEPTS CSV. We can batch up to ?? SKUs per OF21 call.")
    elif r1.get("n") == 1 and r2.get("n") == 1 and r3.get("n") == 1:
        print("❌ sku param SINGLE ONLY. Need to paginate without sku filter or use OF22.")
    else:
        print(f"? Mixed: n1={r1.get('n')} n2={r2.get('n')} n3={r3.get('n')}")
        print("Inspect output above to decide.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
