"""
Cron entry: sync the Feishu Macy-kuyotq-Mirakl pricing config snapshot into
order_system.offer_pricing_config.

This is a pure Feishu -> autoweb DB sync. No Mirakl API calls.

Usage:
    PYTHONPATH=/var/www/autoweb/AutoWeb FLASK_CONFIG=production \
        ./venv/bin/python scripts/sync_feishu_pricing_config.py \
        --store macy_kuyotq
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
from app.services.feishu_pricing_config_service import FEISHU_SOURCES, run_sync


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync Feishu pricing config into order_system.offer_pricing_config"
    )
    parser.add_argument("--store", default="macy_kuyotq", help="store key")
    args = parser.parse_args()

    store_key = str(args.store or "").strip().lower()
    if store_key not in FEISHU_SOURCES:
        print(json.dumps({
            "success": False,
            "msg": f"unsupported store: {store_key}",
            "supported": list(FEISHU_SOURCES.keys()),
        }, ensure_ascii=False))
        return 1

    config_name = os.environ.get("FLASK_CONFIG", "production")
    app = create_app(config_name)
    with app.app_context():
        try:
            result = run_sync(store_key)
            result["success"] = True
        except Exception as exc:
            result = {"success": False, "store_key": store_key, "error": str(exc)}

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
