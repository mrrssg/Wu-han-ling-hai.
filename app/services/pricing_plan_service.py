# -*- coding: utf-8 -*-
"""分档定价评档（Lowes-Autool 起步）——固定四档退货感知定价（用户2026-07-16定案）。

  目标毛利 = 10%基线 + 退货税（预期退货率×货值占售价比），向上取最近档：
  tier_12  12%竞争档：卖得好退货低（退货税≤2个点），最低档保价格竞争力
  tier_15  15%标准档：默认/新品起步
  tier_18  18%高退货档
  delist   下架档：18%都盖不住（退货税>8个点）——提价救不了
  cold_watch / cold_12  零销量：<60天观察；≥60天降到12%档促活（最低就到12%，不再往下）

  预期退货率 = (SKU退货数 + 8×类目率) ÷ (订单数 + 8) —— 群体收缩，1单1退≠100%。
  铺货模式：退货损失按货值算（LGD=成本/售价），海外仓回收不进定价（是额外净赚）。

铁律：
  * 数据全部数据库直读：订单=lowes_order_data，退货=mirakl_returns（不绕飞书）
  * 全集 = offerprice_listing active=1（数据库offer=在卖；飞书只是资料）
  * 评档只写 pricing_tier 表 + 原因 + 证据，绝不直接改价——推送必须人工确认
  * 重评 UPSERT 不覆盖人工 status
"""
import json
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List

from app.models.db_manager import DBManager

CN_TZ = timezone(timedelta(hours=8))

BASELINE = 0.10            # 考核基线（唯一不动的常数）
COLD_WATCH_DAYS = 30       # 零销量<30天=新品观察
MIN_ORDERS_OWN = 11        # 窗口>10单用SKU自己的数（用户2026-07-17定；退货损失率、实际毛利同一门槛）
MIN_CAT_ORDERS = 30        # 运营×类目 / 运营全池 的最小样本单数
MATURE_MIN_AGE = 30        # 统计窗口：下单满30天的订单才进退货损失率统计（用户2026-07-16定）
MATURE_MAX_AGE = 120
KEEP_TOLERANCE = 0.01      # 档位毛利差1个点以内算盖得住（用户定：贴基线换销量稳）
DELIST_MIN_WINDOW_ORDERS = 25   # 判下架必须窗口内>=25单坐实（用户2026-07-16定）

# 三档名义毛利（公式除数 = 1 − 佣金 − 名义毛利）；每档"能给多少毛利"逐SKU现算
TIER_MARGINS = [("tier_12", 0.12), ("tier_15", 0.15), ("tier_18", 0.18)]

OP_PREFIX = {"ATCO-MDLW": "刘梦蝶", "ATCO-MRLW": "明瑞瑞", "ATCO-YCLW": "朱以超"}


def sku_operator(sku: str) -> str:
    return OP_PREFIX.get((sku or "")[:9], "未分配")

STORE_MAP = {  # store_key -> (platform, shop_name, mirakl shop_id, return_case店名)
    "lowes_autool": ("Lowes", "autool", 10, "Lowes-Autool"),
}


def _qall(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params) if params else cur.execute(sql)
        return cur.fetchall() or []


def evaluate_store(store_key: str) -> Dict[str, Any]:
    """v5（用户2026-07-16定稿）：

      需要的毛利 = 10%基线 + 退货损失率
        退货损失率 = (1-p)×成熟口径退货货值×尾部系数M ÷ 成熟口径销售额
        （取数阶梯带运营维度：SKU自己>10单 → 运营×类目 → 运营全池 → 全店）
      各档能给的毛利 = 逐SKU现算：P=该档公式价（divisor=1−佣金−名义档），
        毛利 = (P×(1−佣金) − 成本) ÷ P —— 成本/退货运费/尺寸用SKU自己的，供应商价实时
      分档 = 取能盖住"需要的毛利"的最低档（差1个点内算盖得住）
      下架 = 连18%档公式价的毛利都盖不住 且 窗口>=25单坐实；样本不够先18档观察
      现价对不对不在评档里管——候选管道/价格监控发现现价毛利低于档位目标就修回公式价
    """
    from app.services.repricing_monitor_service import fetch_pricing_configs
    from app.services.repricing_formula import calculate_breakdown

    platform, shop_name, shop_id, rc_store = STORE_MAP[store_key]
    now = datetime.now(CN_TZ)
    conn = DBManager.get_connection()
    try:
        offers = _qall(conn, """
            SELECT shop_sku, warehouse_sku, category, cost_price, last_cost_snapshot,
                   price, origin_price, discount_price, listed_at
            FROM order_system.offerprice_listing
            WHERE platform=%s AND shop_name=%s AND active=1""", (platform, shop_name))

        # ---- 活跃度（近90天全量订单，用于冷启动判定）----
        sku_orders90 = {r["offer_sku"]: r for r in _qall(conn, """
            SELECT offer_sku, COUNT(DISTINCT order_id) AS orders,
                   ROUND(SUM(line_total_price),2) AS sale
            FROM order_system.lowes_order_data
            WHERE shop_id=%s AND order_state<>'CANCELED'
              AND created_date >= DATE_SUB(CURDATE(), INTERVAL 90 DAY)
            GROUP BY offer_sku""", (shop_id,))}

        # ---- 成熟口径销售（下单30~120天前）----
        sku_sale_m = {r["offer_sku"]: r for r in _qall(conn, """
            SELECT offer_sku, COUNT(DISTINCT order_id) AS orders,
                   ROUND(SUM(line_total_price),2) AS sale,
                   ROUND(AVG(DATEDIFF(CURDATE(), created_date))) AS avg_age
            FROM order_system.lowes_order_data
            WHERE shop_id=%s AND order_state<>'CANCELED'
              AND created_date BETWEEN DATE_SUB(CURDATE(), INTERVAL %s DAY)
                                   AND DATE_SUB(CURDATE(), INTERVAL %s DAY)
            GROUP BY offer_sku""", (shop_id, MATURE_MAX_AGE, MATURE_MIN_AGE))}
        win_ages = [int(r["avg_age"] or 60) for r in sku_sale_m.values()]
        avg_age = int(sum(win_ages) / len(win_ages)) if win_ages else 60

        # ---- 成熟口径退货货值（归属到30~120天前下单的订单）----
        sku_ret_m = {r["shop_sku"]: r for r in _qall(conn, """
            SELECT shop_sku, COUNT(*) AS rets, ROUND(SUM(cost),2) AS rval
            FROM order_system.return_case
            WHERE store=%s AND state<>'not_charged'
              AND order_date BETWEEN DATE_SUB(CURDATE(), INTERVAL %s DAY)
                                 AND DATE_SUB(CURDATE(), INTERVAL %s DAY)
            GROUP BY shop_sku""", (rc_store, MATURE_MAX_AGE, MATURE_MIN_AGE))}

        # ---- 尾部补偿系数M：90~150天前下单（≈5月，退货基本到齐）----
        mrow = _qall(conn, """
            SELECT ROUND(SUM(cost),2) AS v_total,
                   ROUND(SUM(CASE WHEN DATEDIFF(return_date, order_date) <= %s
                                  THEN cost ELSE 0 END),2) AS v_early
            FROM order_system.return_case
            WHERE store=%s AND state<>'not_charged'
              AND order_date BETWEEN DATE_SUB(CURDATE(), INTERVAL 150 DAY)
                                 AND DATE_SUB(CURDATE(), INTERVAL 90 DAY)""",
            (avg_age, rc_store))[0]
        v_total = float(mrow["v_total"] or 0)
        v_early = float(mrow["v_early"] or 0)
        tail_m = (v_total / v_early) if v_early > 0 else 1.0
        tail_m = min(tail_m, 2.5)   # 防样本小时系数爆炸

        # ---- p：可要回比例（有跟踪号退货货值占比，近90天退货）----
        prow = _qall(conn, """
            SELECT ROUND(SUM(cost),2) AS total_v,
                   ROUND(SUM(CASE WHEN claim_tracking IS NOT NULL AND claim_tracking<>''
                                  THEN cost ELSE 0 END),2) AS tracked_v
            FROM order_system.return_case
            WHERE store=%s AND state<>'not_charged'
              AND return_date >= DATE_SUB(CURDATE(), INTERVAL 90 DAY)""", (rc_store,))[0]
        total_v = float(prow["total_v"] or 0)
        p_recover = (float(prow["tracked_v"] or 0) / total_v) if total_v > 0 else 0.0
        loss_factor = 1.0 - p_recover

        # ---- 实际毛利（展示列用：飞书利润实测，非退货毛利÷非退货销售额）----
        sku_profit = {r["shop_sku"]: r for r in _qall(conn, """
            SELECT shop_sku, orders, sale, profit_gross, returns_cnt
            FROM order_system.profit_sku_90d WHERE store=%s""", (rc_store,))}
        sku_ret_sale = {r["shop_sku"]: float(r["rsale"] or 0) for r in _qall(conn, """
            SELECT shop_sku, ROUND(SUM(sale),2) AS rsale
            FROM order_system.return_case
            WHERE store=%s AND state<>'not_charged'
              AND return_date >= DATE_SUB(CURDATE(), INTERVAL 90 DAY)
            GROUP BY shop_sku""", (rc_store,))}

        # ---- 供应商实时价（一次性载入，各档公式价都从这里算）----
        price_map = {}
        for sup, table in (("Costway", "autooperate.newestdropship"),
                           ("Vevor", "autooperate.newestdropship_vevor")):
            for r in _qall(conn, f"SELECT SKU, Price FROM {table}"):
                if r["Price"] is not None:
                    price_map[(sup, r["SKU"])] = float(r["Price"])

        # 类目映射用全部offer（含已下架）——历史销售/退货都该计入类目统计，
        # 否则下架SKU的销售掉进"无类目"，类目分母被砍小、损失率虚高
        offer_cat = {r["shop_sku"]: (r["category"] or "(无类目)") for r in _qall(conn, """
            SELECT shop_sku, category FROM order_system.offerprice_listing
            WHERE platform=%s AND shop_name=%s""", (platform, shop_name))}
    finally:
        conn.close()

    configs = fetch_pricing_configs(store_key)

    # ---- 聚合（全部带运营维度——每人过各自的基线、不互相背锅，用户2026-07-17定）----
    op_cat_sale: Dict[tuple, float] = {}
    op_cat_rval: Dict[tuple, float] = {}
    op_cat_orders: Dict[tuple, int] = {}
    op_sale: Dict[str, float] = {}
    op_rval: Dict[str, float] = {}
    op_orders: Dict[str, int] = {}
    for sku, st in sku_sale_m.items():
        cat = offer_cat.get(sku, "(无类目)")
        op = sku_operator(sku)
        s_ = float(st["sale"] or 0)
        n_ = int(st["orders"] or 0)
        op_cat_sale[(op, cat)] = op_cat_sale.get((op, cat), 0.0) + s_
        op_cat_orders[(op, cat)] = op_cat_orders.get((op, cat), 0) + n_
        op_sale[op] = op_sale.get(op, 0.0) + s_
        op_orders[op] = op_orders.get(op, 0) + n_
    for sku, rr in sku_ret_m.items():
        cat = offer_cat.get(sku, "(无类目)")
        op = sku_operator(sku)
        v_ = float(rr["rval"] or 0)
        op_cat_rval[(op, cat)] = op_cat_rval.get((op, cat), 0.0) + v_
        op_rval[op] = op_rval.get(op, 0.0) + v_
    store_sale_m = sum(op_sale.values()) or 1.0
    store_loss = loss_factor * tail_m * sum(op_rval.values()) / store_sale_m

    # 实际毛利（展示列）按同样阶梯聚合
    op_cat_gross: Dict[tuple, float] = {}
    op_cat_netsale: Dict[tuple, float] = {}
    op_gross: Dict[str, float] = {}
    op_netsale: Dict[str, float] = {}
    for sku, pr in sku_profit.items():
        cat = offer_cat.get(sku, "(无类目)")
        op = sku_operator(sku)
        ns = float(pr["sale"] or 0) - sku_ret_sale.get(sku, 0.0)
        if ns > 0:
            g_ = float(pr["profit_gross"] or 0)
            op_cat_gross[(op, cat)] = op_cat_gross.get((op, cat), 0.0) + g_
            op_cat_netsale[(op, cat)] = op_cat_netsale.get((op, cat), 0.0) + ns
            op_gross[op] = op_gross.get(op, 0.0) + g_
            op_netsale[op] = op_netsale.get(op, 0.0) + ns
    store_netsale = sum(op_netsale.values()) or 1.0
    store_am = sum(op_gross.values()) / store_netsale

    def level_pick(sku, cat, op):
        """返回 (退货损失率, 实际毛利[展示], 数据来源, 窗口单数)。
        阶梯：SKU自己(窗口>10单) → 运营×类目(该运营该类目≥30单)
        → 运营全池(该运营≥30单) → 全店。门槛都数满30天窗口内的单"""
        stm = sku_sale_m.get(sku)
        orders_m = int(stm["orders"] or 0) if stm else 0
        if orders_m >= MIN_ORDERS_OWN:
            sale_m = float(stm["sale"] or 0) if stm else 0.0
            pr = sku_profit.get(sku)
            ns = (float(pr["sale"] or 0) - sku_ret_sale.get(sku, 0.0)) if pr else 0.0
            if sale_m > 0 and pr and ns > 0:
                lr = loss_factor * tail_m * (float(sku_ret_m[sku]["rval"]) if sku in sku_ret_m else 0.0) / sale_m
                am = float(pr["profit_gross"] or 0) / ns
                return lr, am, "SKU自己", orders_m
        k = (op, cat)
        if op_cat_orders.get(k, 0) >= MIN_CAT_ORDERS and op_cat_sale.get(k, 0) > 0:
            lr = loss_factor * tail_m * op_cat_rval.get(k, 0.0) / op_cat_sale[k]
            am = (op_cat_gross.get(k, 0.0) / op_cat_netsale[k]) \
                if op_cat_netsale.get(k, 0) > 0 else store_am
            return lr, am, "运营×类目", orders_m
        if op_orders.get(op, 0) >= MIN_CAT_ORDERS and op_sale.get(op, 0) > 0:
            lr = loss_factor * tail_m * op_rval.get(op, 0.0) / op_sale[op]
            am = (op_gross.get(op, 0.0) / op_netsale[op]) \
                if op_netsale.get(op, 0) > 0 else store_am
            return lr, am, "运营全池", orders_m
        return store_loss, store_am, "全店", orders_m

    rows = []
    counts: Dict[str, int] = {}
    for o in offers:
        sku = o["shop_sku"]
        cat = offer_cat[sku]
        op = sku_operator(sku)
        st90 = sku_orders90.get(sku)
        orders90 = int(st90["orders"] or 0) if st90 else 0
        pr = sku_profit.get(sku)
        returns90 = int(pr["returns_cnt"] or 0) if pr else 0
        cost = float(o["cost_price"] or o["last_cost_snapshot"] or 0)
        cur_price = float(o["discount_price"] or o["price"] or o["origin_price"] or 0)
        listed_days = (now.date() - o["listed_at"].date()).days if o["listed_at"] else None

        lr, am, src, orders_m = level_pick(sku, cat, op)
        need = BASELINE + lr

        # ---- 各档公式价的精确毛利：毛利 = (P×(1−佣金) − 成本) ÷ P ----
        tier_margin_map: Dict[str, float] = {}
        cfg = configs.get(o["warehouse_sku"]) if o["warehouse_sku"] else None
        supplier = ((cfg.get("supplier") or "Costway").strip() or "Costway") if cfg else None
        sp = price_map.get((supplier, o["warehouse_sku"])) if cfg else None
        if cfg and sp and cfg.get("commission_rate") is not None \
                and cfg.get("return_shipping_base") is not None \
                and cfg.get("discount_factor") is not None:
            cr = float(cfg["commission_rate"])
            for key, nominal in TIER_MARGINS:
                try:
                    bd = calculate_breakdown(
                        supplier=supplier, supplier_price=sp,
                        return_shipping_base=float(cfg["return_shipping_base"]),
                        discount_factor=float(cfg["discount_factor"]),
                        length_in=float(cfg.get("length_in") or 0),
                        width_in=float(cfg.get("width_in") or 0),
                        height_in=float(cfg.get("height_in") or 0),
                        weight_lb=float(cfg.get("weight_lb") or 0),
                        formula_variant="lowes",
                        divisor_override=(1.0 - cr - nominal))
                    P = float(bd.discount_price)
                    if P > 0:
                        tier_margin_map[key] = (P * (1 - cr) - float(bd.cost)) / P
                except Exception:
                    pass

        ev = {"orders_90d": orders90, "orders_window": orders_m,
              "returns_90d": returns90,
              "loss_rate": round(lr, 4), "need": round(need, 4),
              "actual_margin": round(am, 4), "source": src,
              "cat": cat, "operator": op, "p_recover": round(p_recover, 4),
              "tail_m": round(tail_m, 3), "avg_age": avg_age,
              "tier_margins": {k: round(v, 4) for k, v in tier_margin_map.items()},
              "supplier_price": sp,
              "listed_days": listed_days, "cur_price": cur_price, "cost": cost}

        if orders90 == 0:
            if listed_days is not None and listed_days < COLD_WATCH_DAYS:
                tier, target = "cold_watch", None
                reason = f"上架{listed_days}天还没出单——新品观察期（{COLD_WATCH_DAYS}天内）不动价"
            else:
                tier, target = "cold_12", 0.12
                d = f"{listed_days}天" if listed_days is not None else f"{COLD_WATCH_DAYS}天以上(老批次)"
                reason = f"上架{d}零销量——降到最低档12%促活（最低只到12%）；出单即转正常评档"
        elif not tier_margin_map:
            tier, target = "tier_15", 0.15
            reason = ("缺定价配置或供应商实时价，算不出各档公式价毛利——"
                      "暂按15%标准档持有，数据补齐后自动重评")
        else:
            m_txt = " / ".join(f"{int(n*100)}档{tier_margin_map[k]*100:.1f}%"
                               for k, n in TIER_MARGINS if k in tier_margin_map)
            tier = None
            for key, nominal in TIER_MARGINS:
                m = tier_margin_map.get(key)
                if m is not None and m >= need - KEEP_TOLERANCE:
                    tier, target = key, nominal
                    break
            if tier is not None:
                reason = (f"需要毛利{need*100:.1f}%（10%基线+退货损失率{lr*100:.1f}%，{src}口径，"
                          f"可要回{p_recover*100:.1f}%已折减）；本SKU各档公式价毛利 {m_txt}"
                          f"——{int(target*100)}%档够住，取最低够用档（现实际毛利{am*100:.1f}%）")
            elif orders_m >= DELIST_MIN_WINDOW_ORDERS:
                tier, target = "delist", None
                reason = (f"退货损失率{lr*100:.1f}%（{src}，窗口{orders_m}单坐实），"
                          f"需要毛利{need*100:.1f}%；本SKU各档公式价毛利 {m_txt}"
                          f"——顶到18%档也盖不住，提价救不了，建议下架止血")
            else:
                tier, target = "tier_18", 0.18
                reason = (f"退货损失率{lr*100:.1f}%（{src}）需要毛利{need*100:.1f}%，"
                          f"各档公式价毛利 {m_txt} 都不够——但窗口仅{orders_m}单"
                          f"（<{DELIST_MIN_WINDOW_ORDERS}单不判死），先按18%档卖着攒数据再定")

        counts[tier] = counts.get(tier, 0) + 1
        rows.append((store_key, sku, tier, target, reason,
                     json.dumps(ev, ensure_ascii=False), 0,
                     orders90, returns90, round(am, 4), None,
                     listed_days, cur_price or None, cost or None,
                     round(lr, 4), src, op, now))

    conn = DBManager.get_connection()
    try:
        sql = """
            INSERT INTO order_system.pricing_tier
                (store_key, shop_sku, tier, target_margin, reason_text, evidence_json,
                 data_suspect, orders_90d, returns_90d, margin_90d, gross_margin,
                 listed_days, cur_price, cost_price, loss_rate, rate_source, operator, assigned_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
                tier=VALUES(tier), target_margin=VALUES(target_margin),
                reason_text=VALUES(reason_text), evidence_json=VALUES(evidence_json),
                data_suspect=VALUES(data_suspect), orders_90d=VALUES(orders_90d),
                returns_90d=VALUES(returns_90d), margin_90d=VALUES(margin_90d),
                gross_margin=VALUES(gross_margin), listed_days=VALUES(listed_days),
                cur_price=VALUES(cur_price), cost_price=VALUES(cost_price),
                loss_rate=VALUES(loss_rate), rate_source=VALUES(rate_source),
                operator=VALUES(operator), margin_90d=VALUES(margin_90d),
                assigned_at=VALUES(assigned_at)
        """
        active_skus = {r[1] for r in rows}
        with conn.cursor() as cur:
            # 只清掉"已不在卖"的offer（UPSERT不含status列，人工确认状态在重评中保留）
            cur.execute("SELECT shop_sku FROM order_system.pricing_tier WHERE store_key=%s",
                        (store_key,))
            stale = [r["shop_sku"] for r in cur.fetchall() if r["shop_sku"] not in active_skus]
            for i in range(0, len(stale), 500):
                chunk = stale[i:i + 500]
                ph = ",".join(["%s"] * len(chunk))
                cur.execute(f"DELETE FROM order_system.pricing_tier "
                            f"WHERE store_key=%s AND shop_sku IN ({ph})",
                            [store_key] + chunk)
            for i in range(0, len(rows), 500):
                cur.executemany(sql, rows[i:i + 500])
        conn.commit()
    finally:
        conn.close()
    return {"store_key": store_key, "offers": len(rows), "tiers": counts}


# =====================================================================
# 执行层：按档位目标毛利生成待改价候选（dry_run，人工确认后经OF24推送）
# =====================================================================

MIN_DEVIATION = 0.01     # 目标价和现价差1%以内不折腾
COLD_BATCH = 500   # 零销量降档促活每轮最多放500个候选（分批观察激活率，防一次全动）

ACTION_TIERS = ("tier_12", "tier_15", "tier_18", "cold_12")   # 现价偏离档位公式价≥1%才出候选


def generate_plan_candidates(store_key: str) -> Dict[str, Any]:
    """把 pricing_tier 的档位目标翻译成待改价候选（status=dry_run, run_id=plan-*）。
    只生成候选，绝不直接改价——推送仍走 candidates 页人工确认 + OF24 管道。
    目前只支持 lowes 系店铺（divisor = 1 − 佣金 − 目标毛利 的语义是lowes公式的）。"""
    if not store_key.startswith("lowes"):
        raise ValueError("plan candidates 目前只支持 lowes 系店铺")

    from app.services.repricing_monitor_service import (
        fetch_active_offers, fetch_pricing_configs, _insert_log)
    from app.services.repricing_formula import calculate_breakdown, realised_margin

    now = datetime.now(CN_TZ)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    run_id = f"plan-{store_key}-{now.strftime('%Y%m%d%H%M%S')}"
    log_rows: List[Any] = []

    conn = DBManager.get_connection()
    try:
        tiers = {r["shop_sku"]: r for r in _qall(conn, """
            SELECT shop_sku, tier, target_margin FROM order_system.pricing_tier
            WHERE store_key=%s AND tier IN %s AND target_margin IS NOT NULL""",
            (store_key, ACTION_TIERS))}
        blacklist = {r["shop_sku"] for r in _qall(conn, """
            SELECT shop_sku FROM order_system.offer_alert_state WHERE blacklisted=1""")}
        # 供应商价格一次性载入（逐SKU查会把3000个SKU拖成半小时）
        price_map = {}
        for sup, table in (("Costway", "autooperate.newestdropship"),
                           ("Vevor", "autooperate.newestdropship_vevor")):
            for r in _qall(conn, f"SELECT SKU, Price FROM {table}"):
                if r["Price"] is not None:
                    price_map[(sup, r["SKU"])] = float(r["Price"])
    finally:
        conn.close()

    offers = fetch_active_offers(store_key)
    configs = fetch_pricing_configs(store_key)
    summary = {"run_id": run_id, "tier_skus": len(tiers), "candidates": 0,
               "skip_no_cfg": 0, "skip_no_supplier_price": 0, "skip_small_dev": 0,
               "skip_blacklist": 0, "skip_no_price": 0, "clamped": 0,
               "by_tier": {}}

    for ctx in offers:
        t = tiers.get(ctx.shop_sku)
        if not t:
            continue
        if (t["tier"] == "cold_12"
                and summary["by_tier"].get("cold_12", 0) >= COLD_BATCH):
            summary["skip_cold_deferred"] = summary.get("skip_cold_deferred", 0) + 1
            continue
        if ctx.shop_sku in blacklist:
            summary["skip_blacklist"] += 1
            continue
        cfg = configs.get(ctx.warehouse_sku)
        if not cfg or cfg.get("discount_factor") is None \
                or cfg.get("commission_rate") is None \
                or cfg.get("return_shipping_base") is None:
            summary["skip_no_cfg"] += 1
            continue
        supplier = (cfg.get("supplier") or "Costway").strip() or "Costway"
        supplier_price = price_map.get((supplier, ctx.warehouse_sku))
        if not supplier_price:
            summary["skip_no_supplier_price"] += 1
            continue
        discount_factor = float(cfg["discount_factor"])
        commission = float(cfg["commission_rate"])
        target_margin = float(t["target_margin"])
        cur_discount = ctx.db_discount_price or (
            ctx.db_origin_price * discount_factor if ctx.db_origin_price else None)
        if not cur_discount or cur_discount <= 0:
            summary["skip_no_price"] += 1
            continue

        divisor = 1.0 - commission - target_margin
        bd = calculate_breakdown(
            supplier=supplier, supplier_price=float(supplier_price),
            return_shipping_base=float(cfg["return_shipping_base"]),
            discount_factor=discount_factor,
            length_in=float(cfg.get("length_in") or 0),
            width_in=float(cfg.get("width_in") or 0),
            height_in=float(cfg.get("height_in") or 0),
            weight_lb=float(cfg.get("weight_lb") or 0),
            formula_variant="lowes", divisor_override=divisor)
        # 2026-07-16定案：不设限幅——价格只按用户公式算，涨跌幅在候选页展示，人工确认把关
        target_discount = round(bd.discount_price, 2)
        dev = (target_discount - cur_discount) / cur_discount
        clamp_note = ""
        if abs(dev) < MIN_DEVIATION:
            summary["skip_small_dev"] += 1
            continue
        target_origin = round(target_discount / discount_factor, 2)

        margin_before = realised_margin(
            current_origin_price=ctx.db_origin_price or target_origin,
            supplier=supplier, supplier_price=float(supplier_price),
            return_shipping_base=float(cfg["return_shipping_base"]),
            discount_factor=discount_factor, commission_rate=commission,
            length_in=float(cfg.get("length_in") or 0),
            width_in=float(cfg.get("width_in") or 0),
            height_in=float(cfg.get("height_in") or 0),
            weight_lb=float(cfg.get("weight_lb") or 0))

        log_rows.append({
            "run_id": run_id, "run_type": "plan", "store_key": store_key,
            "shop_sku": ctx.shop_sku, "warehouse_sku": ctx.warehouse_sku,
            "triggered_at": now_str, "status": "dry_run",
            "decision_reason": (
                f"[定价方案·{t['tier']}] 目标毛利{target_margin:.0%}："
                f"现折扣价${cur_discount:.2f}→${target_discount:.2f}"
                f"（{dev:+.1%}）{clamp_note}"),
            "supplier": supplier,
            "supplier_price_db": float(supplier_price),
            "old_origin_price": ctx.db_origin_price,
            "old_discount_price": ctx.db_discount_price,
            "old_cost": ctx.last_cost_snapshot,
            "new_cost": round(bd.cost, 4),
            "discount_factor": discount_factor,
            "commission_rate": commission,
            "return_shipping_base": float(cfg["return_shipping_base"]),
            "profit_margin_before": round(margin_before, 4),
            "profit_margin_after": round(target_margin, 4),
            "new_origin_price": target_origin,
            "new_discount_price": target_discount,
            "target_origin_price": target_origin,
        })
        summary["candidates"] += 1
        summary["by_tier"][t["tier"]] = summary["by_tier"].get(t["tier"], 0) + 1

    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            # 上一轮plan候选（含中断残留）作废：只保留最新一轮；
            # 已推价的成功记录在别的run_id（batch-/manual-）里，不受影响
            cursor.execute(
                """DELETE FROM order_system.offer_price_change_log
                   WHERE store_key=%s AND status='dry_run' AND run_id LIKE %s""",
                (store_key, f"plan-{store_key}-%"))
            for row in log_rows:
                _insert_log(cursor, row)
        conn.commit()
    finally:
        conn.close()
    return summary
