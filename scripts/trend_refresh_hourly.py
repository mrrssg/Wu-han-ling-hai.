# -*- coding: utf-8 -*-
"""每小时轻量刷新最近2天的销售/净赚（profit_trend_daily），让首页跟上飞书白天新单。

Usage:
    PYTHONPATH=/var/www/autoweb/AutoWeb FLASK_CONFIG=production \
        ./venv/bin/python scripts/trend_refresh_hourly.py
"""
import json
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app import create_app


def main() -> int:
    config_name = os.environ.get("FLASK_CONFIG", "production")
    app = create_app(config_name)
    with app.app_context():
        from app.services.profit_control_service import refresh_recent_trend
        try:
            result = refresh_recent_trend(days_back=2)
            result["success"] = True
        except Exception as exc:
            import traceback
            traceback.print_exc()
            result = {"success": False, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
