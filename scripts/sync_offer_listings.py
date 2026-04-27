import argparse
import json
import os
import sys

from app import create_app
from app.services.offer_listing_sync_service import STORE_CONFIGS, run_offer_listing_sync


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Mirakl offer listings to offerprice_listing.")
    parser.add_argument("--store", required=True, help="store key: macy_kuyotq | macy_wopet")
    args = parser.parse_args()

    store_key = str(args.store or "").strip().lower()
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
        result = run_offer_listing_sync(store_key=store_key)

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
