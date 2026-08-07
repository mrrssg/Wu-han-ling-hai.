# -*- coding: utf-8 -*-
"""每日供应商库存同步(可 cron)= 网页「同步供应商库存」的脚本版。

下载 Costway/Vevor 等 feed → 写本地库存镜像(newestdropship 等),
维护 first_seen/restock_at(新品/新补货识别)。只刷本地镜像,不推平台。
安排在每日候选池重建(7:00)之前跑,让重建能吃到当天的新品/补货。
"""
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import create_app
from app.services.stock_service import StockService


def main() -> int:
    app = create_app(os.environ.get("FLASK_CONFIG", "production"))
    with app.app_context():
        result = StockService.sync_all_suppliers()
        print("[sync_supplier_stock] result:", result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
