# -*- coding: utf-8 -*-
"""蓝海类目一键刷新(全服务器端, 可 cron): 内部邻接/货盘 + 卖家精灵季节。

= compute_blue_ocean(内部fit/supply, 写表) + 对 top-N 候选直连 SellerSprite
google_trend 取季节 + _season 折季节标签 + 重算 blue_score。不再需要人工喂数据。

用法: refresh_blue_ocean.py [store=autool] [topn=24]
需要 instance/sellersprite_key.txt(或环境变量 SS_KEY)。
"""
import json
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
from compute_blue_ocean import _kw, compute
from apply_blue_ocean_season import FACTOR, _profile, _season
from sellersprite_client import google_trend, market_research


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
                prof = json.dumps(_profile(items))
                internal = 0.65 * fit + 0.35 * sup
                blue = round(min(100, internal * FACTOR.get(tag, 1.0)))
                if fit < 60:
                    blue = min(blue, 48)
                if fit < 15:
                    blue = min(blue, 22)
                # 需求验证(Amazon 泛需求参考):只对进面板的强邻接类目查,省 credits
                mr = market_research(kw) if fit >= 55 else {}
                cur.execute(
                    "UPDATE order_system.lowes_blue_ocean SET season_tag=%s, season_peak=%s,"
                    " trend_now=%s, season_profile=%s, blue_score=%s, gt_keyword=%s,"
                    " amz_units=%s, amz_revenue=%s, amz_price=%s, amz_return=%s, amz_node=%s"
                    " WHERE store=%s AND lowes_leaf=%s",
                    (tag, peak, now, prof, blue, kw,
                     mr.get("units"), mr.get("revenue"), mr.get("price"), mr.get("return_rate"),
                     mr.get("node"), store, leaf))
                done += 1
                print(f"  {tag:6s} peak={peak or '-':4s} now={now if now is not None else '-':>3} "
                      f"blue={blue:3d} pts={len(items):2d} amz_units={mr.get('units')} "
                      f"ret={mr.get('return_rate')} | {leaf} (kw={kw})")
                time.sleep(1.2)             # 限速, 防 SellerSprite 限流

            # 探索区: 邻接弱(fit<55, 进不了主推)但货盘厚的类目 → 查 Amazon 需求,
            # 供"高需求新赛道"人工判断(蹦床/宠物等我们没沾过但市场大的)
            cur.execute("SELECT lowes_leaf, gt_keyword FROM order_system.lowes_blue_ocean "
                        "WHERE store=%s AND fit_score<55 AND sku_n>=30 AND amz_units IS NULL "
                        "ORDER BY sku_n DESC LIMIT 15", (store,))
            explore = cur.fetchall()
            exp_done = 0
            for e in explore:
                kw = e["gt_keyword"]
                mr = market_research(kw)
                items = google_trend(kw)                       # 探索区也给旺季
                tag, peak, now = _season(items)
                prof = json.dumps(_profile(items))
                cur.execute(
                    "UPDATE order_system.lowes_blue_ocean SET amz_units=%s, amz_revenue=%s,"
                    " amz_price=%s, amz_return=%s, amz_node=%s,"
                    " season_tag=%s, season_peak=%s, trend_now=%s, season_profile=%s"
                    " WHERE store=%s AND lowes_leaf=%s",
                    (mr.get("units"), mr.get("revenue"), mr.get("price"), mr.get("return_rate"),
                     mr.get("node"), tag, peak, now, prof, store, e["lowes_leaf"]))
                exp_done += 1
                print(f"  [探索] units={mr.get('units')} ret={mr.get('return_rate')} "
                      f"{tag} 旺{peak} node={mr.get('node')} | {e['lowes_leaf']} (kw={kw})")
                time.sleep(1.2)

            # 已售类目旺季(我们自有历史仅5-6月不够判季节 → 用 google_trend 5年)
            cur.execute("SELECT lowes_leaf FROM order_system.lowes_cat_demand "
                        "WHERE store=%s AND gmv>0 ORDER BY gmv DESC LIMIT 35", (store,))
            sold = cur.fetchall()
            sold_done = 0
            for s in sold:
                leaf = s["lowes_leaf"]
                items = google_trend(_kw(leaf))
                tag, peak, now = _season(items)
                prof = json.dumps(_profile(items))
                cur.execute("UPDATE order_system.lowes_cat_demand SET season_tag=%s,"
                            " season_peak=%s, trend_now=%s, season_profile=%s"
                            " WHERE store=%s AND lowes_leaf=%s",
                            (tag, peak, now, prof, store, leaf))
                sold_done += 1
                print(f"  [已售] {tag} 旺{peak or '-'} now={now} | {leaf}")
                time.sleep(1.2)
        conn.commit()
        print(f"[blue_ocean refresh] store={store} candidates={len(cand)} season_updated={done} "
              f"explore_probed={exp_done} sold_season={sold_done}")
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
