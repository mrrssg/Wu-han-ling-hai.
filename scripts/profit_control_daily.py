# -*- coding: utf-8 -*-
"""利润控制台每日聚合 cron 入口。

拉飞书订单表 + mirakl_returns → 重建退货三态台账 → cell快照(链梯修正) → 问题规则引擎。

Usage:
    PYTHONPATH=/var/www/autoweb/AutoWeb FLASK_CONFIG=production \
        ./venv/bin/python scripts/profit_control_daily.py
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
        from app.services.profit_control_service import run_daily_aggregation
        try:
            result = run_daily_aggregation()
            result["success"] = True
        except Exception as exc:
            import traceback
            traceback.print_exc()
            result = {"success": False, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
