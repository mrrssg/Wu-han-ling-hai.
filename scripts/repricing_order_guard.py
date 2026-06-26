"""
Cron entry: 成交价对账哨兵 (order-price guard).

Scans recent, non-cancelled orders for each store, records any line whose REAL
sale price would be a loss / below 5% margin into order_system.order_guard_alert,
and pushes new findings to the Feishu group bot once.

This is ALERT-ONLY — it never calls a Mirakl write API.

Usage:
    # all 4 active Mirakl stores (default), hourly cron
    PYTHONPATH=/var/www/autoweb/AutoWeb FLASK_CONFIG=production \
        ./venv/bin/python scripts/repricing_order_guard.py

    # one store
    ... scripts/repricing_order_guard.py --store lowes_autool

    # record but don't push (e.g. first backfill run)
    ... scripts/repricing_order_guard.py --no-notify --lookback-hours 168
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
from app.services.order_guard_service import run_order_guard, run_all, DEFAULT_LOOKBACK_HOURS


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the order-price guard.")
    parser.add_argument("--store", default="all",
                        help="store_key, or 'all' (default) for every active store")
    parser.add_argument("--lookback-hours", type=int, default=DEFAULT_LOOKBACK_HOURS)
    parser.add_argument("--no-notify", action="store_true",
                        help="record alerts but do not push to Feishu")
    args = parser.parse_args()

    config_name = os.environ.get("FLASK_CONFIG", "production")
    app = create_app(config_name)
    with app.app_context():
        try:
            if args.store == "all":
                result = {"success": True, "stores": run_all(
                    lookback_hours=args.lookback_hours, notify=not args.no_notify)}
            else:
                result = run_order_guard(
                    args.store, lookback_hours=args.lookback_hours,
                    notify=not args.no_notify)
        except Exception as exc:
            result = {"success": False, "error": str(exc)}

    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
