# -*- coding: utf-8 -*-
"""达标恒温器·周体检（cron，2026-07-28）。

每周测各 Lowes cell(店铺×运营)的**成熟净利率**(下单已满、退货到齐的两个月) vs 10%基线，
再看"执行待改价页的提价"能补多少，给出判定与建议：
  达标 / 提价即可达标 / 需上调档或下架(提价也盖不住)
**只体检+建议，不自动改价**——推价仍走 /repricing 待改价页人工确认（守定价铁律）。
结果写 thermostat_weekly，页面 /thermostat 看。
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import create_app
from app.models.db_manager import DBManager

BASELINE = 0.10
CN = timezone(timedelta(hours=8))
STORES = {"Lowes-Autool": "lowes_autool", "Lowes-Yasonic": "lowes_yasonic"}
OP_PREFIX = (("MDLW", "刘梦蝶"), ("MRLW", "明瑞瑞"), ("YCLW", "朱以超"))


def _prev_month(dt, k):
    y, m = dt.year, dt.month - k
    while m <= 0:
        m += 12
        y -= 1
    return f"{y:04d}-{m:02d}"


def _op_of(sku):
    s = sku or ""
    for pfx, name in OP_PREFIX:
        if pfx in s:
            return name
    return "其他"


def main() -> int:
    app = create_app(os.environ.get("FLASK_CONFIG", "production"))
    with app.app_context():
        now = datetime.now(CN)
        m1, m2 = _prev_month(now, 1), _prev_month(now, 2)
        check_date = now.date()
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cur:
                # ① 各cell成熟净利+退货损失(前两个整月，退货基本到齐)
                cur.execute("""SELECT store, operator,
                       ROUND(SUM(sale),2) sale, ROUND(SUM(net),2) net,
                       ROUND(SUM(loss_expected),2) loss
                    FROM order_system.profit_month_cohort
                    WHERE store IN ('Lowes-Autool','Lowes-Yasonic')
                      AND order_month IN (%s,%s)
                    GROUP BY store, operator""", (m1, m2))
                cells = {(r["store"], r["operator"]):
                         {"sale": float(r["sale"] or 0), "net": float(r["net"] or 0),
                          "loss": float(r["loss"] or 0), "up": 0.0, "n": 0, "sale90": 0.0}
                         for r in cur.fetchall()}

                # ② 各cell近90天销售额(算提价加几个点的分母)
                cur.execute("""SELECT store, shop_sku, sale FROM order_system.profit_sku_90d
                               WHERE store IN ('Lowes-Autool','Lowes-Yasonic')""")
                for r in cur.fetchall():
                    key = (r["store"], _op_of(r["shop_sku"]))
                    if key in cells:
                        cells[key]["sale90"] += float(r["sale"] or 0)

                # ③ 最新一轮plan的待提价候选 → 每cell可补回毛利
                sku_sale90 = {(r["store"], r["shop_sku"]): float(r["sale"] or 0)
                              for r in _sku_sale(cur)}
                for sk, mb, ma, store in _plan_uplift(cur):
                    store_name = "Lowes-Autool" if store == "lowes_autool" else "Lowes-Yasonic"
                    key = (store_name, _op_of(sk))
                    if key not in cells:
                        continue
                    sale = sku_sale90.get((store_name, sk), 0.0)
                    cells[key]["up"] += (ma - mb) * sale
                    cells[key]["n"] += 1

                # ④ 判定 + 写库
                #   核心结构信号=成熟退货损失率 vs 顶档覆盖上限(顶档18%名义≈22%毛利−10%基线=12%)
                #   退货率≤12%: 定价能覆盖,靠待改价提价+v5成熟单周转达标(不是靠现在这点补回一步到位)
                #   退货率>12%: 顶档也盖不住,结构性亏,要减量/下架高退货SKU
                TOP_COVER = 0.12
                rows = []
                for (store, op), c in sorted(cells.items()):
                    sale, net = c["sale"], c["net"]
                    margin = net / sale if sale > 0 else 0.0
                    gap = BASELINE - margin
                    loss_rate = (c["loss"] / sale) if sale > 0 else 0.0
                    base90 = c["sale90"] or sale or 1.0
                    pts = c["up"] / base90
                    est_after = margin + pts
                    if gap <= 0.001:
                        verdict = "达标"
                        sug = f"成熟净利率{margin*100:.1f}%已达标，维持。"
                    elif loss_rate > TOP_COVER:
                        verdict = "退货率过高·需减量下架"
                        sug = (f"缺口{gap*100:.1f}点；成熟退货损失率{loss_rate*100:.1f}%>顶档能覆盖的12%，"
                               f"提价也盖不住(顶档18%名义≈22%毛利−损失{loss_rate*100:.1f}%<10%)。"
                               f"必须下架/减量该运营高退货SKU，查pricing_tier该运营loss_rate最高的几个。")
                    else:
                        verdict = "定价可覆盖·提价+周转达标"
                        sug = (f"缺口{gap*100:.1f}点；退货损失率{loss_rate*100:.1f}%在顶档可覆盖范围(<12%)。"
                               f"成熟净利低是老定价遗留——①去/repricing待改价页把{c['n']}个待提价SKU推掉"
                               f"(即补${c['up']:,.0f}≈+{pts*100:.1f}点)；②等v5成熟单周转到位,净利率会自然爬向10%。")
                    rows.append((check_date, store, op, f"{m2}+{m1}", round(sale, 2), round(net, 2),
                                 round(margin, 4), round(gap, 4), c["n"], round(c["up"], 2),
                                 round(pts, 4), round(est_after, 4), verdict, sug[:800]))

                for r in rows:
                    cur.execute("""INSERT INTO order_system.thermostat_weekly
                        (check_date,store,operator,mature_months,mature_sale,mature_net,
                         mature_margin,gap,reprice_skus,reprice_uplift,reprice_points,
                         est_margin_after,verdict,suggestion)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON DUPLICATE KEY UPDATE mature_sale=VALUES(mature_sale),
                          mature_net=VALUES(mature_net), mature_margin=VALUES(mature_margin),
                          gap=VALUES(gap), reprice_skus=VALUES(reprice_skus),
                          reprice_uplift=VALUES(reprice_uplift), reprice_points=VALUES(reprice_points),
                          est_margin_after=VALUES(est_margin_after), verdict=VALUES(verdict),
                          suggestion=VALUES(suggestion)""", r)
            conn.commit()
        finally:
            conn.close()

        print(f"=== 恒温器体检 {check_date} (成熟月 {m2}+{m1}) ===")
        for r in rows:
            print(f"  {r[1]} {r[2]}: 成熟净利{r[6]*100:.1f}% 缺口{r[7]*100:+.1f} "
                  f"[{r[12]}] 提价{r[8]}个/+{r[10]*100:.1f}点→{r[11]*100:.1f}%")
    return 0


def _sku_sale(cur):
    cur.execute("""SELECT store, shop_sku, sale FROM order_system.profit_sku_90d
                   WHERE store IN ('Lowes-Autool','Lowes-Yasonic')""")
    return cur.fetchall()


def _plan_uplift(cur):
    """最新一轮 plan 的提价候选：(shop_sku, margin_before, margin_after, store_key)。"""
    cur.execute("""SELECT shop_sku, profit_margin_before mb, profit_margin_after ma, store_key
        FROM order_system.offer_price_change_log
        WHERE store_key IN ('lowes_autool','lowes_yasonic')
          AND run_type='plan' AND status='dry_run'
          AND triggered_at >= DATE_SUB(NOW(), INTERVAL 30 HOUR)
          AND profit_margin_after > profit_margin_before + 0.005""")
    return [(r["shop_sku"], float(r["mb"] or 0), float(r["ma"] or 0), r["store_key"])
            for r in cur.fetchall()]


if __name__ == "__main__":
    sys.exit(main())
