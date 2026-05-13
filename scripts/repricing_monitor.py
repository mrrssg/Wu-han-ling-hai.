"""
Part 1 cron entry: run the daily repricing monitor for one store.

Default is dry_run=True so no OF24 writes happen until the operator opts in
with --live. Even in dry-run mode the full audit log lands in
offer_price_change_log so the operator can review what would have been pushed.

Usage:
    # dry-run (no Mirakl writes) - default
    PYTHONPATH=/var/www/autoweb/AutoWeb FLASK_CONFIG=production \
        ./venv/bin/python scripts/repricing_monitor.py --store macy_kuyotq

    # production with live OF24 pushes
    PYTHONPATH=/var/www/autoweb/AutoWeb FLASK_CONFIG=production \
        ./venv/bin/python scripts/repricing_monitor.py --store macy_kuyotq --live
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
from app.services.repricing_monitor_service import run_monitor


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Part 1 repricing monitor.")
    parser.add_argument("--store", default="macy_kuyotq")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Actually call OF24 (default is dry-run with full audit log only).",
    )
    args = parser.parse_args()

    config_name = os.environ.get("FLASK_CONFIG", "production")
    app = create_app(config_name)
    with app.app_context():
        try:
            result = run_monitor(store_key=args.store, dry_run=not args.live)
            result.setdefault("success", True)
        except Exception as exc:
            result = {"success": False, "error": str(exc)}

    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
