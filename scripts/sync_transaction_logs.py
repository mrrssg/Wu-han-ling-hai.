import argparse
import json
import os
import sys

from app import create_app
from app.services.transaction_log_sync_service import STORE_CONFIGS, MAX_PAGES_PER_RUN, run_transaction_log_sync


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Mirakl transaction logs for a store.")
    parser.add_argument("--store", required=True, help="store key, e.g. macy_kuyotq")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES_PER_RUN, help="max pages per run")
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
        result = run_transaction_log_sync(
            store_key=store_key,
            max_pages=args.max_pages,
        )

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
