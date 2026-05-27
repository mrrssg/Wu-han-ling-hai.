"""Fast mirakl_listing backfill via OF21 list-mode (100 offers/page).

For one store, paginate the entire shop via OF21 and batch-upsert
mirakl_listing + offerprice_listing.category. ~170x faster than
backfill_listing.py (which is OF21 by-sku); use this for the initial
4-store backfill or any time you need to refresh the whole shop.

Each store routes through its own Brightdata pinned IP via
mirakl_offer_api_service._proxy_session_headers.

Usage:
    ./venv/bin/python scripts/backfill_listing_fast.py --store macy_kuyotq
"""
import argparse
import json
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app import create_app
from app.services.offer_listing_sync_service import (
    STORE_CONFIGS,
    backfill_via_of21_list,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True,
                        help="store_key: macy_kuyotq | macy_wopet | lowes_autool | lowes_yasonic")
    parser.add_argument("--sleep", type=float, default=0.5,
                        help="seconds between pages (default 0.5)")
    args = parser.parse_args()

    store_key = args.store.strip().lower()
    if store_key not in STORE_CONFIGS:
        print(json.dumps({
            "success": False,
            "msg": f"invalid store: {store_key}",
            "supported": list(STORE_CONFIGS.keys()),
        }, ensure_ascii=False))
        return 1

    config_name = os.environ.get("FLASK_CONFIG", "production")
    app = create_app(config_name)
    with app.app_context():
        result = backfill_via_of21_list(
            store_key, sleep_between_pages=args.sleep,
        )

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
