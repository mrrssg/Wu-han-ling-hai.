# -*- coding: utf-8 -*-
"""Lowes 蓝海类目 —— 叠加季节分(卖家精灵 google_trend 原始数据 → 季节标签 + 综合分)。

输入 JSON: { "<lowes_leaf>": {"keyword": "...", "items": [[time_ms, value], ...]}, ... }
items = google_trend 返回的月度序列(近~5年)。本脚本把它折成 12 个日历月的季节曲线,
判当前处于旺季/升温/平稳/降温,取峰值月,并重算 blue_score = 内部分 × 季节系数。

季节系数: ⏫升温=1.25  🔥当季=1.15  🟰平稳=1.0  ⏬降温=0.7 (无数据=1.0)
内部分 = 0.6*fit_score + 0.4*supply_score (从表里读回, 与 compute 一致)。
弱适配(fit<15)仍硬压 blue_score≤25。

用法: apply_blue_ocean_season.py <season.json> [store=autool]
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import create_app
from app.models.db_manager import DBManager

FACTOR = {"⏫升温": 1.25, "🔥当季": 1.15, "🟰平稳": 1.0, "⏬降温": 0.7}


def _season(items):
    """月度序列 → (tag, peak_month_label, trend_now_index)。"""
    prof = {m: [] for m in range(1, 13)}
    for it in items or []:
        try:
            if isinstance(it, dict):
                t, v = it["time"], float(it["value"])
            else:
                t, v = it[0], float(it[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        m = datetime.utcfromtimestamp(t / 1000).month
        prof[m].append(v)
    avg = {m: (sum(vs) / len(vs)) for m, vs in prof.items() if vs}
    if not avg:
        return "—", None, None
    peak_m = max(avg, key=avg.get)
    peak_v = avg[peak_m]
    cur = datetime.now().month
    nowv = avg.get(cur, 0.0)

    def at(off):
        return avg.get(((cur - 1 + off) % 12) + 1, 0.0)

    nxt = max(at(1), at(2))
    trend = (nxt - nowv) / nowv if nowv > 1e-6 else 0.0
    if peak_v <= 0:
        tag = "—"
    elif nowv >= 0.85 * peak_v:
        tag = "🔥当季"
    elif trend >= 0.20:
        tag = "⏫升温"
    elif trend <= -0.15:
        tag = "⏬降温"
    else:
        tag = "🟰平稳"
    return tag, f"{peak_m}月", round(nowv)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: apply_blue_ocean_season.py <season.json> [store]")
        return 2
    path = sys.argv[1]
    store = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] in ("autool", "yasonic") else "autool"
    data = json.loads(Path(path).read_text(encoding="utf-8"))

    app = create_app(os.environ.get("FLASK_CONFIG", "production"))
    with app.app_context():
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT lowes_leaf, fit_score, supply_score "
                            "FROM order_system.lowes_blue_ocean WHERE store=%s", (store,))
                base = {r["lowes_leaf"]: (int(r["fit_score"] or 0), int(r["supply_score"] or 0))
                        for r in cur.fetchall()}
                done = 0
                for leaf, payload in data.items():
                    if leaf not in base:
                        continue
                    fit, sup = base[leaf]
                    if payload.get("tag"):     # 预算好的季节标签(本次种子)
                        tag = payload["tag"]
                        peak = payload.get("peak")
                        now = payload.get("now")
                    else:                       # 原始 google_trend 序列(未来刷新)
                        tag, peak, now = _season(payload.get("items"))
                    internal = 0.65 * fit + 0.35 * sup
                    blue = round(min(100, internal * FACTOR.get(tag, 1.0)))
                    if fit < 60:        # 非同L2弱邻接封顶(与 compute 一致,季节也不抬)
                        blue = min(blue, 48)
                    if fit < 15:
                        blue = min(blue, 22)
                    cur.execute(
                        "UPDATE order_system.lowes_blue_ocean SET season_tag=%s, season_peak=%s,"
                        " trend_now=%s, blue_score=%s WHERE store=%s AND lowes_leaf=%s",
                        (tag, peak, now, blue, store, leaf))
                    done += 1
                    print(f"  {tag:6s} peak={peak or '-':4s} now={now if now is not None else '-':>3} "
                          f"blue={blue:3d} | {leaf}")
            conn.commit()
            print(f"[blue_ocean season] store={store} updated={done}/{len(data)}")
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
