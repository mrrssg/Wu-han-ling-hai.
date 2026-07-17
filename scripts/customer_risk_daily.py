# -*- coding: utf-8 -*-
"""可疑客户档案每日重建（cron 06:15）。"""
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import create_app


def main() -> int:
    app = create_app(os.environ.get("FLASK_CONFIG", "production"))
    with app.app_context():
        from app.services.customer_risk_service import rebuild_profiles
        print(rebuild_profiles())
    return 0


if __name__ == "__main__":
    sys.exit(main())
