# -*- coding: utf-8 -*-
"""Macy 蓝海一键刷新(全服务器端, 可 cron): 内部邻接/货盘 + 卖家精灵季节/需求。先 kuyotq。

= compute_macy_blue_ocean + 对候选查 google_trend(季节)+ market_research(Amazon需求)
+ 探索区(邻接弱货盘厚)+ 已售类目旺季(macy_cat_demand)。复用 Lowes 的 sellersprite/季节工具。
用法: refresh_macy_blue_ocean.py [store=kuyotq] [topn=24]。需 instance/sellersprite_key.txt。
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
from compute_blue_ocean import _kw
from compute_macy_blue_ocean import compute
from apply_blue_ocean_season import FACTOR, _profile, _season
from sellersprite_client import google_trend, market_research


def refresh(store: str, topn: int):
    cand = compute(store, topn)
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
                mr = market_research(kw) if fit >= 55 else {}
                cur.execute(
                    "UPDATE order_system.macy_blue_ocean SET season_tag=%s, season_peak=%s,"
                    " trend_now=%s, season_profile=%s, blue_score=%s, gt_keyword=%s,"
                    " amz_units=%s, amz_revenue=%s, amz_price=%s, amz_return=%s, amz_node=%s"
                    " WHERE store=%s AND macy_leaf=%s",
                    (tag, peak, now, prof, blue, kw,
                     mr.get("units"), mr.get("revenue"), mr.get("price"), mr.get("return_rate"),
                     mr.get("node"), store, leaf))
                done += 1
                print(f"  {tag:6s} 旺{peak or '-':4s} blue={blue:3d} amz={mr.get('units')} | {leaf}")
                time.sleep(1.2)

            # 探索区: 邻接弱(fit<55)但货盘厚(sku>=30)→ 查 Amazon 需求 + 季节
            cur.execute("SELECT macy_leaf, gt_keyword FROM order_system.macy_blue_ocean "
                        "WHERE store=%s AND fit_score<55 AND sku_n>=30 AND amz_units IS NULL "
                        "ORDER BY sku_n DESC LIMIT 15", (store,))
            exp = 0
            for e in cur.fetchall():
                kw = e["gt_keyword"]
                mr = market_research(kw)
                items = google_trend(kw)
                tag, peak, now = _season(items)
                prof = json.dumps(_profile(items))
                cur.execute(
                    "UPDATE order_system.macy_blue_ocean SET amz_units=%s, amz_revenue=%s,"
                    " amz_price=%s, amz_return=%s, amz_node=%s,"
                    " season_tag=%s, season_peak=%s, trend_now=%s, season_profile=%s"
                    " WHERE store=%s AND macy_leaf=%s",
                    (mr.get("units"), mr.get("revenue"), mr.get("price"), mr.get("return_rate"),
                     mr.get("node"), tag, peak, now, prof, store, e["macy_leaf"]))
                exp += 1
                print(f"  [探索] amz={mr.get('units')} node={mr.get('node')} | {e['macy_leaf']}")
                time.sleep(1.2)

            # 已售类目旺季(自有历史短→google_trend 5年)
            cur.execute("SELECT macy_leaf FROM order_system.macy_cat_demand "
                        "WHERE store=%s AND gmv>0 ORDER BY gmv DESC LIMIT 35", (store,))
            sold = 0
            for s in cur.fetchall():
                leaf = s["macy_leaf"]
                items = google_trend(_kw(leaf))
                tag, peak, now = _season(items)
                prof = json.dumps(_profile(items))
                cur.execute("UPDATE order_system.macy_cat_demand SET season_tag=%s,"
                            " season_peak=%s, trend_now=%s, season_profile=%s"
                            " WHERE store=%s AND macy_leaf=%s", (tag, peak, now, prof, store, leaf))
                sold += 1
                print(f"  [已售] {tag} 旺{peak or '-'} | {leaf}")
                time.sleep(1.2)
        conn.commit()
        print(f"[macy_blue_ocean refresh] store={store} cand={len(cand)} "
              f"season_updated={done} explore={exp} sold_season={sold}")
    finally:
        conn.close()


def main() -> int:
    store, topn = "kuyotq", 24
    for a in sys.argv[1:]:
        if a.isdigit():
            topn = int(a)
        elif a in ("kuyotq",):
            store = a
    app = create_app(os.environ.get("FLASK_CONFIG", "production"))
    with app.app_context():
        refresh(store, topn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
