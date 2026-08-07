# -*- coding: utf-8 -*-
"""Macy 蓝海类目推荐 —— 内部计算(邻接适配 + 货盘厚度)。先 kuyotq。

蓝海 = macy_selection_pool 有货、但该 Macy 类目 0 销量(不在 macy_cat_demand.gmv>0)。
邻接: Macy 4级路径(L1/L2/L3/leaf)。同 L3(叶子直接父级)有已赚钱类目=强(60~100);
      仅同 L2=弱(20~38,blue封顶48);无邻接 fit=8 blue≤22。证据=已售类目 net×gmv。
货盘: SKU数(log)+有图率+库存。blue=0.65fit+0.35supply。
季节/需求由 refresh_macy_blue_ocean.py 另加。
"""
import math
import os
import sys
from collections import defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

from app import create_app
from app.models.db_manager import DBManager
from compute_blue_ocean import _kw   # 复用关键词逻辑 + KEYWORD_OVERRIDE

FIT_QUERY_MIN = 25


def compute(store: str = "kuyotq", topn: int = 24):
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            # 1) 已赚钱类目(net>0) + full_path
            cur.execute("""
                SELECT d.macy_leaf AS leaf, d.margin_rate AS net, d.gmv, c.full_path
                FROM order_system.macy_cat_demand d
                JOIN order_system.macy_leaf_category c ON c.leaf=d.macy_leaf
                WHERE d.store=%s AND d.gmv>0 AND d.margin_rate>0
                GROUP BY d.macy_leaf, d.margin_rate, d.gmv, c.full_path""", (store,))
            l3_w, l2_w = defaultdict(float), defaultdict(float)
            l3_top, l2_top = {}, {}
            for s in cur.fetchall():
                net = float(s["net"] or 0)
                w = net * float(s["gmv"] or 0)
                if w <= 0:
                    continue
                parts = [x for x in (s["full_path"] or "").split("/") if x]
                l2 = parts[1] if len(parts) > 1 else None
                l3 = parts[2] if len(parts) > 2 else None
                if l3:
                    l3_w[l3] += w
                    if l3 not in l3_top or net > l3_top[l3][1]:
                        l3_top[l3] = (s["leaf"], net)
                if l2:
                    l2_w[l2] += w
                    if l2 not in l2_top or net > l2_top[l2][1]:
                        l2_top[l2] = (s["leaf"], net)
            max_l3 = max(l3_w.values(), default=1.0) or 1.0
            max_l2 = max(l2_w.values(), default=1.0) or 1.0

            # 2) 蓝海类目聚合 + path（价格是字符串,数字提取求均值）
            cur.execute("""
                SELECT p.macy_leaf AS leaf, MAX(p.macy_brand) AS brand,
                       COUNT(*) AS sku_n, SUM(p.has_overview_img=1) AS with_img,
                       ROUND(AVG(p.stock),0) AS avg_stock,
                       ROUND(AVG(NULLIF(CAST(REGEXP_REPLACE(p.price,'[^0-9.]','') AS DECIMAL(12,2)),0)),2) AS avg_price,
                       MAX(c.full_path) AS full_path
                FROM order_system.macy_selection_pool p
                LEFT JOIN order_system.macy_cat_demand d
                  ON d.store=%s AND d.macy_leaf=p.macy_leaf AND d.gmv>0
                LEFT JOIN order_system.macy_leaf_category c ON c.leaf=p.macy_leaf
                WHERE p.macy_leaf IS NOT NULL AND d.macy_leaf IS NULL
                GROUP BY p.macy_leaf""", (store,))
            ws = cur.fetchall()

            rows = []
            for w in ws:
                leaf = w["leaf"]
                parts = [x for x in (w["full_path"] or "").split("/") if x]
                l1 = parts[0] if parts else None
                l2 = parts[1] if len(parts) > 1 else None
                l3 = parts[2] if len(parts) > 2 else None
                sku_n = int(w["sku_n"] or 0)
                with_img = int(w["with_img"] or 0)
                avg_stock = int(w["avg_stock"] or 0)
                avg_price = float(w["avg_price"]) if w["avg_price"] else None

                strong = False
                if l3 and l3 in l3_w:
                    fit = 60 + 40 * min(l3_w[l3] / max_l3, 1.0)
                    strong = True
                    sib, sn = l3_top[l3]
                    reason = f"挨着已验证的{sib}(净{sn*100:.0f}%·同「{l3}」)"
                elif l2 and l2 in l2_w:
                    fit = 20 + 18 * min(l2_w[l2] / max_l2, 1.0)
                    sib, sn = l2_top[l2]
                    reason = f"仅同「{l2}」(弱邻接)·{sib}(净{sn*100:.0f}%)"
                else:
                    fit = 8
                    reason = "主业无邻接类目(慎入)"
                fit_score = round(fit)

                sku_comp = min(math.log10(sku_n + 1) / math.log10(200), 1.0)
                stock_comp = min(avg_stock / 200.0, 1.0)
                img_rate = (with_img / sku_n) if sku_n else 0.0
                supply_score = round(100 * (0.50 * sku_comp + 0.20 * stock_comp + 0.30 * img_rate))

                internal = 0.65 * fit_score + 0.35 * supply_score
                blue = round(internal)
                if not strong:
                    blue = min(blue, 48)
                if fit_score < 15:
                    blue = min(blue, 22)

                rows.append({
                    "leaf": leaf, "l1": l1, "l2": l2, "l3": l3, "brand": w["brand"],
                    "sku_n": sku_n, "with_img": with_img, "avg_price": avg_price,
                    "avg_stock": avg_stock, "fit_score": fit_score, "fit_reason": reason[:255],
                    "supply_score": supply_score, "gt_keyword": _kw(leaf),
                    "blue_score": blue, "internal": internal,
                })

            cur.execute("DELETE FROM order_system.macy_blue_ocean WHERE store=%s", (store,))
            ins = ("INSERT INTO order_system.macy_blue_ocean "
                   "(store,macy_leaf,l1,l2,l3,brand,sku_n,with_img,avg_price,avg_stock,"
                   "fit_score,fit_reason,supply_score,gt_keyword,blue_score) VALUES "
                   "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)")
            cur.executemany(ins, [
                (store, r["leaf"], r["l1"], r["l2"], r["l3"], r["brand"], r["sku_n"],
                 r["with_img"], r["avg_price"], r["avg_stock"], r["fit_score"], r["fit_reason"],
                 r["supply_score"], r["gt_keyword"], r["blue_score"]) for r in rows])
        conn.commit()
    finally:
        conn.close()

    cand = sorted([r for r in rows if r["fit_score"] >= FIT_QUERY_MIN],
                  key=lambda r: r["internal"], reverse=True)[:topn]
    print(f"[macy_blue_ocean] store={store} whitespace={len(rows)} season_candidates={len(cand)}")
    for r in sorted(rows, key=lambda r: r["blue_score"], reverse=True)[:12]:
        print(f"  {r['blue_score']:3d} fit{r['fit_score']:3d} sup{r['supply_score']:3d} "
              f"{r['sku_n']:4d}SKU | {r['leaf']} <{r['fit_reason']}>")
    return cand


def main() -> int:
    store, topn = "kuyotq", 24
    for a in sys.argv[1:]:
        if a.isdigit():
            topn = int(a)
        elif a in ("kuyotq",):
            store = a
    app = create_app(os.environ.get("FLASK_CONFIG", "production"))
    with app.app_context():
        compute(store, topn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
