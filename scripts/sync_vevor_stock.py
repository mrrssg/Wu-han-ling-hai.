# -*- coding: utf-8 -*-
"""每日司顺(Vevor)库存 feed 同步(可 cron)= 网页「同步供应商库存」的 Vevor 部分。

下载 Vevor 主 feed + 分仓 feed → 写 newestdropship_vevor(司顺库存/价格,喂定价与推平台库存)。
只刷本地镜像,不推平台。与 Costway(sync_supplier_stock.py)分开跑,避免某供应商卡死拖累另一个。
注意:司顺「选品feed」(vevor_feed)是另一张表,由 sync_vevor_feed.py 单独维护。
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
        # 主库存
        t0 = time.time()
        print("[vevor] downloading main feed...", flush=True)
        xb = StockService.download_xlsx(StockService.URL_VEVOR)
        print(f"[vevor] main downloaded in {time.time()-t0:.1f}s", flush=True)
        if xb:
            t1 = time.time()
            ok, msg = StockService.process_vevor_data(xb)
            print(f"[vevor] main db done in {time.time()-t1:.1f}s ok={ok} | {msg}", flush=True)
        else:
            print("[vevor] main download FAILED", flush=True)
        # 分仓库存(NJ/CA)
        try:
            t2 = time.time()
            print("[vevor] downloading warehouse feed...", flush=True)
            xw = StockService.download_xlsx(StockService.URL_VEVOR_WAREHOUSE)
            print(f"[vevor] warehouse downloaded in {time.time()-t2:.1f}s", flush=True)
            if xw:
                t3 = time.time()
                okw, msgw = StockService.process_vevor_warehouse_data(xw)
                print(f"[vevor] warehouse db done in {time.time()-t3:.1f}s ok={okw} | {msgw}", flush=True)
            else:
                print("[vevor] warehouse download FAILED", flush=True)
        except Exception as exc:
            print(f"[vevor] warehouse error: {str(exc)[:160]}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
