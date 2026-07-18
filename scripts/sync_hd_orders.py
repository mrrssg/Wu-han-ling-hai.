# -*- coding: utf-8 -*-
"""HD订单同步入口。用法：
  python scripts/sync_hd_orders.py            # 增量：未发货全量+近3天已发货（cron每小时）
  python scripts/sync_hd_orders.py --backfill 120   # 历史回填
"""
import argparse
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import create_app


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--backfill", type=int, default=0)
    args = ap.parse_args()
    app = create_app(os.environ.get("FLASK_CONFIG", "production"))
    with app.app_context():
        from app.services.hd_order_service import sync_hd_orders, backfill_hd_orders
        if args.backfill:
            print(backfill_hd_orders(days=args.backfill))
        print(sync_hd_orders(days=args.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
