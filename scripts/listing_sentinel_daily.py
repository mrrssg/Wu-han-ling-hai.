# -*- coding: utf-8 -*-
"""Listing哨兵 cron 入口：对近N天有退货的SKU做三方listing对比。

Usage:
    PYTHONPATH=/var/www/autoweb/AutoWeb FLASK_CONFIG=production \
        ./venv/bin/python scripts/listing_sentinel_daily.py [--days 1] [--limit 0]
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--skus", type=str, default="",
                        help="逗号分隔的Shop SKU列表, 定向重审(忽略days窗口)")
    args = parser.parse_args()

    app = create_app(os.environ.get("FLASK_CONFIG", "production"))
    with app.app_context():
        from app.services.listing_sentinel_service import run_sentinel
        try:
            skus = [s.strip() for s in args.skus.split(",") if s.strip()] or None
            result = run_sentinel(str(_PROJECT_ROOT), days=args.days,
                                  limit=args.limit, skus=skus)
            result["success"] = True
        except Exception as exc:
            import traceback
            traceback.print_exc()
            result = {"success": False, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
