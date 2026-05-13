import argparse
import json
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app import create_app
from app.services.offer_listing_sync_service import STORE_CONFIGS, run_offer_listing_sync


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync Mirakl offer listings via OF52+OF53 into offerprice_listing."
    )
    parser.add_argument("--store", required=True, help="store key: macy_kuyotq")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Ignore cursor and force a full export (use this for the bootstrap run).",
    )
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
        result = run_offer_listing_sync(store_key=store_key, force_full=args.full)

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
