import argparse
import json
import os
import sys

from app import create_app
from app.services.mirakl_sync_service import (
    DEFAULT_MAX,
    get_sync_store_options,
    is_sync_store_supported,
    run_order_sync_job,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Mirakl order sync for a single store.")
    parser.add_argument("--store", default="macy_kuyotq", help="store key, e.g. macy_kuyotq")
    parser.add_argument("--max", type=int, default=DEFAULT_MAX, help="page max size (1-100)")
    args = parser.parse_args()

    store_key = str(args.store or "").strip().lower()
    if not is_sync_store_supported(store_key):
        print(
            json.dumps(
                {
                    "success": False,
                    "msg": f"invalid store: {store_key}",
                    "supported": list(get_sync_store_options().keys()),
                },
                ensure_ascii=False,
            )
        )
        return 1

    config_name = os.environ.get("FLASK_CONFIG", "production")
    app = create_app(config_name)
    with app.app_context():
        result = run_order_sync_job(
            store_key=store_key,
            run_type="auto",
            trigger_source="cron",
            max_value=args.max,
        )

    print(json.dumps(result, ensure_ascii=False))
    if result.get("success") or result.get("status") == "skipped":
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
