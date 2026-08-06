# -*- coding: utf-8 -*-
"""蓝海类目一键刷新(全服务器端, 可 cron): 内部邻接/货盘 + 卖家精灵季节。

= compute_blue_ocean(内部fit/supply, 写表) + 对 top-N 候选直连 SellerSprite
google_trend 取季节 + _season 折季节标签 + 重算 blue_score。不再需要人工喂数据。

用法: refresh_blue_ocean.py [store=autool] [topn=24]
需要 instance/sellersprite_key.txt(或环境变量 SS_KEY)。
"""
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

from app import create_app
from app.models.db_manager import DBManager
from compute_blue_ocean import compute
from apply_blue_ocean_season import FACTOR, _season
from sellersprite_client import google_trend


def refresh(store: str, topn: int):
    cand = compute(store, topn)            # 写内部分, 返回待查季节候选
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            done = 0
            for c in cand:
                leaf, kw = c["leaf"], c["gt_keyword"]
                fit, sup = int(c["fit_score"]), int(c["supply_score"])
                items = google_trend(kw)
                tag, peak, now = _season(items)
                internal = 0.65 * fit + 0.35 * sup
                blue = round(min(100, internal * FACTOR.get(tag, 1.0)))
                if fit < 60:
                    blue = min(blue, 48)
                if fit < 15:
                    blue = min(blue, 22)
                cur.execute(
                    "UPDATE order_system.lowes_blue_ocean SET season_tag=%s, season_peak=%s,"
                    " trend_now=%s, blue_score=%s, gt_keyword=%s WHERE store=%s AND lowes_leaf=%s",
                    (tag, peak, now, blue, kw, store, leaf))
                done += 1
                print(f"  {tag:6s} peak={peak or '-':4s} now={now if now is not None else '-':>3} "
                      f"blue={blue:3d} pts={len(items):2d} | {leaf} (kw={kw})")
                time.sleep(0.4)              # 轻微限速
        conn.commit()
        print(f"[blue_ocean refresh] store={store} candidates={len(cand)} season_updated={done}")
    finally:
        conn.close()


def main() -> int:
    store, topn = "autool", 24
    for a in sys.argv[1:]:
        if a.isdigit():
            topn = int(a)
        elif a in ("autool", "yasonic"):
            store = a
    app = create_app(os.environ.get("FLASK_CONFIG", "production"))
    with app.app_context():
        refresh(store, topn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
