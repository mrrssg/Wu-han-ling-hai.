"""
Second probe: how fast can OF21 paginate the whole macy_kuyotq store, and
does each page include `description` + `internal_description`?

If OF21 truly has no published rate cap, we can pull all active offers'
full data in seconds, then batch-push via OF24 without any OF22 hops.
"""
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


def _call(params, label, skip_cooldown: bool = False):
    """Probe-only helper. By default uses cooldown lock; set skip_cooldown=True
    to deliberately measure Mirakl's true throughput.
    """
    api = _resolve_api(STORE)
    net = _proxy_session_headers(STORE, api["api_key"])
    if not skip_cooldown:
        acquire_offers_cooldown(STORE, action=f"probe_pg_{label}")
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
    return resp, dt


def main() -> int:
    config_name = os.environ.get("FLASK_CONFIG", "production")
    app = create_app(config_name)

    with app.app_context():
        print("=== Probe: page_size=100, offset=0 ===")
        resp, dt = _call({"max": 100, "offset": 0, "paginate": "true"}, "p1")
        print(f"  HTTP {resp.status_code} ({dt:.1f}s)")
        if resp.status_code == 200:
            data = resp.json()
            total = data.get("total_count")
            offers = data.get("offers", [])
            print(f"  total_count: {total}")
            print(f"  offers in this page: {len(offers)}")
            if offers:
                o = offers[0]
                print(f"  first offer keys (sorted): {sorted(o.keys())}")
                print(f"  description present: {'description' in o}, value preview: {str(o.get('description'))[:120]!r}")
                print(f"  internal_description present: {'internal_description' in o}, value preview: {str(o.get('internal_description'))[:120]!r}")
                print(f"  product_references: {o.get('product_references')}")
                print(f"  retail_prices: {o.get('retail_prices')}")
                # Count how many have non-empty description in this page
                nonempty_desc = sum(1 for x in offers if str(x.get('description') or '').strip())
                print(f"  offers with non-empty description in this page: {nonempty_desc}/{len(offers)}")

        # Probe more pages back-to-back WITHOUT cooldown to measure true rate
        print("\n=== Probe: 4 more pages back-to-back, NO cooldown lock ===")
        for i, offset in enumerate([100, 200, 300, 400]):
            resp, dt = _call({"max": 100, "offset": offset, "paginate": "true"},
                              f"pn{i}", skip_cooldown=True)
            data = resp.json() if resp.status_code == 200 else {}
            print(f"  offset={offset}  HTTP {resp.status_code} ({dt:.1f}s)  n={len(data.get('offers', []))}")
            if resp.status_code != 200:
                print(f"    body[:300]: {resp.text[:300]}")
                break
            time.sleep(0.5)   # small courtesy gap

    return 0


if __name__ == "__main__":
    sys.exit(main())
