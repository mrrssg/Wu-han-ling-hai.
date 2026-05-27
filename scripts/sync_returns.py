"""CLI for RT11 returns sync (mirrors sync_transaction_logs.py)."""
import argparse
import json
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app import create_app
from app.services.returns_sync_service import (
    STORE_CONFIGS,
    MAX_PAGES_PER_RUN,
    run_returns_sync,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Mirakl returns (RT11) for a store.")
    parser.add_argument("--store", required=True,
                        help="store_key: macy_kuyotq | macy_wopet | lowes_autool | lowes_yasonic")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES_PER_RUN,
                        help=f"max API calls per run (default {MAX_PAGES_PER_RUN})")
    parser.add_argument("--full-from", default=None,
                        help="ISO UTC string to force-pull from, e.g. 2025-11-27T00:00:00Z; "
                             "ignores stored cursor. Use for the first-time backfill.")
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
        result = run_returns_sync(
            store_key=store_key,
            max_pages=args.max_pages,
            full_from=args.full_from,
        )

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
