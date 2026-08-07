# -*- coding: utf-8 -*-
"""每日 Costway 库存 feed 同步(可 cron)= 网页「同步供应商库存」的 Costway 部分。

下载 Costway dropship feed → 写 newestdropship,维护 first_seen/restock_at
(autool 新品/新补货识别)。只刷本地镜像,不推平台。安排在每日候选池重建(7:00)之前跑。
仅做 Costway(newestdropship)——司顺选品 feed 由 sync_vevor_feed.py 单独维护。
"""
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import create_app
from app.services.stock_service import StockService


def main() -> int:
    app = create_app(os.environ.get("FLASK_CONFIG", "production"))
    with app.app_context():
        from flask import current_app
        base_dir = current_app.config.get("BASE_DIR", os.getcwd())
        cfg = StockService.load_hd_config(base_dir)
        pwd = cfg.get("costway_zip_password", "")
        pwds = [pwd] if pwd else None
        t0 = time.time()
        print("[costway] downloading feed...", flush=True)
        csv_text = StockService.download_csv(StockService.URL_COSTWAY, passwords=pwds)
        print(f"[costway] downloaded {len(csv_text) if csv_text else 0} chars in {time.time()-t0:.1f}s", flush=True)
        if not csv_text:
            print("[costway] EMPTY/FAILED download", flush=True)
            return 1
        t1 = time.time()
        ok, msg = StockService.process_costway_data(csv_text)
        print(f"[costway] db done in {time.time()-t1:.1f}s ok={ok} | {msg}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
