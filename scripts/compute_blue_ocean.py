# -*- coding: utf-8 -*-
"""Lowes 蓝海类目推荐 —— 内部计算(邻接适配 + 货盘厚度)。

蓝海类目 = 供应商有现货(在 lowes_selection_pool)、能归到某 Lowes 类目、
但我们该类目 0 销量(不在 lowes_cat_demand.gmv>0)。

打两个内部分:
- fit_score(邻接适配): 该类目和我们「已赚钱类目」是否同 Lowes 大类。
    同 L2(二级)有已验证赚钱类目 → 强(60~100); 只同 L1(一级) → 中(25~55);
    都没有 → 弱(~8, 沉底)。权重= 已售类目 净利率×GMV(只算 net>0 的做适配证据)。
- supply_score(货盘厚度): SKU数(log) + 有图率 + 库存 + 价格带合理性。

季节分由第二步(卖家精灵 google_trend)另算,见 apply_blue_ocean_season.py。
本脚本把内部分写入 lowes_blue_ocean(season 留空, blue_score=内部分), 并打印
待查季节的 top-N 类目(TOPN_JSON) 供主程序调 MCP。

用法: compute_blue_ocean.py [store=autool] [topn=24]
"""
import json
import math
import os
import sys
from collections import defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import create_app
from app.models.db_manager import DBManager

FIT_QUERY_MIN = 25   # 只对 fit>=此值(至少同L1邻接)的类目查季节，省 credits


def _kw(leaf: str) -> str:
    """类目名 → Google Trends 关键词(小写, 砍掉 &/, 后半, 保留主词)。"""
    s = (leaf or "").lower()
    for sep in ("&", ",", "/"):
        if sep in s:
            s = s.split(sep)[0]
    return s.strip()


def compute(store: str, topn: int):
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            # 1) 已赚钱类目(net>0) + 其 L1/L2, 作邻接证据
            cur.execute("""
                SELECT d.lowes_leaf AS leaf, d.margin_rate AS net, d.gmv, c.l1, c.l2
                FROM order_system.lowes_cat_demand d
                JOIN order_system.lowes_leaf_category c ON c.leaf = d.lowes_leaf
                WHERE d.store=%s AND d.gmv>0 AND d.margin_rate>0
                GROUP BY d.lowes_leaf, d.margin_rate, d.gmv, c.l1, c.l2""", (store,))
            sold = cur.fetchall()
            l2_w, l1_w = defaultdict(float), defaultdict(float)
            l2_top, l1_top = {}, {}
            for s in sold:
                net = float(s["net"] or 0)
                w = net * float(s["gmv"] or 0)
                if w <= 0:
                    continue
                l2, l1 = s["l2"], s["l1"]
                l2_w[l2] += w
                l1_w[l1] += w
                if l2 not in l2_top or net > l2_top[l2][1]:
                    l2_top[l2] = (s["leaf"], net)
                if l1 not in l1_top or net > l1_top[l1][1]:
                    l1_top[l1] = (s["leaf"], net)
            max_l2 = max(l2_w.values(), default=1.0) or 1.0
            max_l1 = max(l1_w.values(), default=1.0) or 1.0

            # 2) 蓝海类目聚合(豪雅有货、我们0销量)
            cur.execute("""
                SELECT p.lowes_leaf AS leaf, COUNT(*) AS sku_n,
                       SUM(p.has_overview_img=1) AS with_img,
                       ROUND(AVG(p.price),2) AS avg_price,
                       ROUND(AVG(p.stock),0) AS avg_stock,
                       MAX(p.lowes_path) AS lowes_path
                FROM order_system.lowes_selection_pool p
                LEFT JOIN order_system.lowes_cat_demand d
                  ON d.store=p.store AND d.lowes_leaf=p.lowes_leaf AND d.gmv>0
                WHERE p.store=%s AND p.lowes_leaf IS NOT NULL AND d.lowes_leaf IS NULL
                GROUP BY p.lowes_leaf""", (store,))
            ws = cur.fetchall()

            rows = []
            for w in ws:
                leaf = w["leaf"]
                path = w["lowes_path"] or ""
                parts = [x for x in path.split("/") if x]
                l1 = parts[0] if parts else None
                l2 = parts[1] if len(parts) > 1 else None
                sku_n = int(w["sku_n"] or 0)
                with_img = int(w["with_img"] or 0)
                avg_price = float(w["avg_price"] or 0)
                avg_stock = int(w["avg_stock"] or 0)

                # fit：同 L2(二级)=真邻接(强); 仅同 L1(一级)=弱(L1如Recreation太宽,
                # 卖过露营椅不代表能卖玩具/蹦床)。strong 决定是否封顶。
                strong = False
                if l2 and l2 in l2_w:
                    fit = 60 + 40 * min(l2_w[l2] / max_l2, 1.0)
                    strong = True
                    sib, sn = l2_top[l2]
                    reason = f"挨着已验证的{sib}(净{sn*100:.0f}%·同「{l2}」)"
                elif l1 and l1 in l1_w:
                    fit = 20 + 18 * min(l1_w[l1] / max_l1, 1.0)
                    sib, sn = l1_top[l1]
                    reason = f"仅同「{l1}」大类(弱邻接)·{sib}(净{sn*100:.0f}%)"
                else:
                    fit = 8
                    reason = "主业无邻接类目(慎入)"
                fit_score = round(fit)

                # supply
                sku_comp = min(math.log10(sku_n + 1) / math.log10(200), 1.0)
                stock_comp = min(avg_stock / 200.0, 1.0)
                img_rate = (with_img / sku_n) if sku_n else 0.0
                if avg_price <= 0 or avg_price < 20:
                    price_comp = 0.5
                elif avg_price <= 400:
                    price_comp = 1.0
                elif avg_price <= 800:
                    price_comp = 0.7
                else:
                    price_comp = 0.4
                supply_score = round(100 * (0.40 * sku_comp + 0.15 * stock_comp
                                            + 0.25 * img_rate + 0.20 * price_comp))

                internal = 0.65 * fit_score + 0.35 * supply_score
                blue = round(internal)
                if not strong:          # 非同L2弱邻接封顶,别让大货盘(Toys 1020SKU)把它抬进推荐
                    blue = min(blue, 48)
                if fit_score < 15:      # 无邻接硬压底
                    blue = min(blue, 22)

                rows.append({
                    "leaf": leaf, "l1": l1, "l2": l2, "sku_n": sku_n,
                    "with_img": with_img, "avg_price": avg_price or None,
                    "avg_stock": avg_stock, "fit_score": fit_score,
                    "fit_reason": reason[:255], "supply_score": supply_score,
                    "gt_keyword": _kw(leaf), "blue_score": blue,
                    "internal": internal,
                })

            # 3) 写表(season 留空)
            cur.execute("DELETE FROM order_system.lowes_blue_ocean WHERE store=%s", (store,))
            ins = ("INSERT INTO order_system.lowes_blue_ocean "
                   "(store,lowes_leaf,l1,l2,sku_n,with_img,avg_price,avg_stock,"
                   "fit_score,fit_reason,supply_score,gt_keyword,blue_score) VALUES "
                   "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)")
            cur.executemany(ins, [
                (store, r["leaf"], r["l1"], r["l2"], r["sku_n"], r["with_img"],
                 r["avg_price"], r["avg_stock"], r["fit_score"], r["fit_reason"],
                 r["supply_score"], r["gt_keyword"], r["blue_score"]) for r in rows])
        conn.commit()
    finally:
        conn.close()

    # 4) 打印待查季节的 top-N(fit>=门槛, 按内部分排)
    cand = sorted([r for r in rows if r["fit_score"] >= FIT_QUERY_MIN],
                  key=lambda r: r["internal"], reverse=True)[:topn]
    print(f"[blue_ocean] store={store} whitespace_cats={len(rows)} "
          f"season_candidates={len(cand)}")
    strong = sorted(rows, key=lambda r: r["blue_score"], reverse=True)[:15]
    print("--- 内部分 Top15(未叠季节) ---")
    for r in strong:
        print(f"  {r['blue_score']:3d} | fit{r['fit_score']:3d} sup{r['supply_score']:3d} "
              f"| {r['sku_n']:4d}SKU | {r['leaf']}  <{r['fit_reason']}>")
    topn_json = [{"leaf": r["leaf"], "keyword": r["gt_keyword"]} for r in cand]
    print("TOPN_JSON:" + json.dumps(topn_json, ensure_ascii=False))


def main() -> int:
    store = "autool"
    topn = 24
    for a in sys.argv[1:]:
        if a.isdigit():
            topn = int(a)
        elif a in ("autool", "yasonic"):
            store = a
    app = create_app(os.environ.get("FLASK_CONFIG", "production"))
    with app.app_context():
        compute(store, topn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
