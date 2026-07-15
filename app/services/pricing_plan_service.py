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
MIN_ORDERS_OWN = 15        # >=15单用SKU自己的数（退货损失率、实际毛利同一门槛）
MIN_CAT_ORDERS = 30
MATURE_MIN_AGE = 30        # 统计窗口：下单满30天的订单才进退货损失率统计（用户2026-07-16定）
MATURE_MAX_AGE = 120
UPLIFT_PER_TIER = 0.03     # 名义档每升3个点，实际毛利约升3个点

# 档位（keep=不动价；不足才升档补差；名义18实际约21还盖不住 → 下架）
BUMP_TIERS = [
    ("tier_15", 0.15, 0.03),   # (名义目标, 可补的实际差距上限)
    ("tier_18", 0.18, 0.06),
]

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
    """v4（用户2026-07-16定案）：

      需要的毛利 = 10%基线 + 退货损失率
        退货损失率 = (1-p) × 成熟口径退货货值 × 尾部系数M ÷ 成熟口径销售额
        成熟口径 = 只统计下单满30天(30~120天前)的订单；
        M = 5月成熟订单实测（平均同龄时点已退货值 → 最终货值 的放大倍数）
      现有实际毛利 = 实测（公式12%定价实际约16%），三级同损失率
      判定：实际>=需要 → keep不动价；缺口<=3点 → 15档；<=6点 → 18档；再大 → 下架
      数据三级：SKU>=15单用自己 → 类目 → 全店；按运营(前缀MDLW/MRLW/YCLW)分池标注
    """
    platform, shop_name, shop_id, rc_store = STORE_MAP[store_key]
    now = datetime.now(CN_TZ)
    conn = DBManager.get_connection()
    try:
        offers = _qall(conn, """
            SELECT shop_sku, category, cost_price, last_cost_snapshot,
                   price, origin_price, discount_price, listed_at
            FROM order_system.offerprice_listing
            WHERE platform=%s AND shop_name=%s AND active=1""", (platform, shop_name))

        # ---- 活跃度（近90天全量订单，用于冷启动判定和"多少单"门槛）----
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

        # ---- 实际毛利（飞书利润实测：非退货毛利 ÷ 非退货销售额）----
        sku_profit = {r["shop_sku"]: r for r in _qall(conn, """
            SELECT shop_sku, orders, sale, profit_gross, returns_cnt
            FROM order_system.profit_sku_90d WHERE store=%s""", (rc_store,))}
        sku_ret_sale = {r["shop_sku"]: float(r["rsale"] or 0) for r in _qall(conn, """
            SELECT shop_sku, ROUND(SUM(sale),2) AS rsale
            FROM order_system.return_case
            WHERE store=%s AND state<>'not_charged'
              AND return_date >= DATE_SUB(CURDATE(), INTERVAL 90 DAY)
            GROUP BY shop_sku""", (rc_store,))}

        offer_cat = {o["shop_sku"]: (o["category"] or "(无类目)") for o in offers}

        # ---- 类目/全店聚合：损失率 与 实际毛利 ----
        cat_sale_m: Dict[str, float] = {}
        cat_rval_m: Dict[str, float] = {}
        cat_orders_m: Dict[str, int] = {}
        for sku, st in sku_sale_m.items():
            cat = offer_cat.get(sku, "(无类目)")
            cat_sale_m[cat] = cat_sale_m.get(cat, 0.0) + float(st["sale"] or 0)
            cat_orders_m[cat] = cat_orders_m.get(cat, 0) + int(st["orders"] or 0)
        for sku, rr in sku_ret_m.items():
            cat = offer_cat.get(sku, "(无类目)")
            cat_rval_m[cat] = cat_rval_m.get(cat, 0.0) + float(rr["rval"] or 0)
        store_sale_m = sum(cat_sale_m.values()) or 1.0
        store_loss = loss_factor * tail_m * sum(cat_rval_m.values()) / store_sale_m

        cat_gross: Dict[str, float] = {}
        cat_netsale: Dict[str, float] = {}
        for sku, pr in sku_profit.items():
            cat = offer_cat.get(sku, "(无类目)")
            ns = float(pr["sale"] or 0) - sku_ret_sale.get(sku, 0.0)
            if ns > 0:
                cat_gross[cat] = cat_gross.get(cat, 0.0) + float(pr["profit_gross"] or 0)
                cat_netsale[cat] = cat_netsale.get(cat, 0.0) + ns
        store_netsale = sum(cat_netsale.values()) or 1.0
        store_am = sum(cat_gross.values()) / store_netsale

        def level_pick(sku, orders90, cat):
            """返回 (损失率, 实际毛利, 数据来源)。
            门槛用"满30天窗口内的单数"——损失率是在窗口里算的，样本就得数窗口的"""
            stm = sku_sale_m.get(sku)
            orders_m = int(stm["orders"] or 0) if stm else 0
            if orders_m >= MIN_ORDERS_OWN:
                sale_m = float(stm["sale"] or 0) if stm else 0.0
                pr = sku_profit.get(sku)
                ns = (float(pr["sale"] or 0) - sku_ret_sale.get(sku, 0.0)) if pr else 0.0
                if sale_m > 0 and pr and ns > 0:
                    lr = loss_factor * tail_m * (float(sku_ret_m[sku]["rval"]) if sku in sku_ret_m else 0.0) / sale_m
                    am = float(pr["profit_gross"] or 0) / ns
                    return lr, am, "SKU自己"
            if cat_orders_m.get(cat, 0) >= MIN_CAT_ORDERS and cat_netsale.get(cat, 0) > 0                     and cat_sale_m.get(cat, 0) > 0:
                lr = loss_factor * tail_m * cat_rval_m.get(cat, 0.0) / cat_sale_m[cat]
                am = cat_gross.get(cat, 0.0) / cat_netsale[cat]
                return lr, am, "类目"
            return store_loss, store_am, "全店"

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

            lr, am, src = level_pick(sku, orders90, cat)
            need = BASELINE + lr
            gap = need - am

            ev = {"orders_90d": orders90, "returns_90d": returns90,
                  "loss_rate": round(lr, 4), "actual_margin": round(am, 4),
                  "need": round(need, 4), "gap": round(gap, 4), "source": src,
                  "cat": cat, "operator": op, "p_recover": round(p_recover, 4),
                  "tail_m": round(tail_m, 3), "avg_age": avg_age,
                  "listed_days": listed_days, "cur_price": cur_price, "cost": cost}

            if orders90 == 0:
                if listed_days is not None and listed_days < COLD_WATCH_DAYS:
                    tier, target = "cold_watch", None
                    reason = f"上架{listed_days}天还没出单——新品观察期（{COLD_WATCH_DAYS}天内）不动价"
                else:
                    tier, target = "cold_12", 0.12
                    d = f"{listed_days}天" if listed_days is not None else f"{COLD_WATCH_DAYS}天以上(老批次)"
                    reason = f"上架{d}零销量——降到最低档12%促活（最低只到12%）；出单即转正常评档"
            elif gap <= 0:
                tier, target = "keep", None
                reason = (f"需要毛利{need*100:.1f}%（10%基线+退货损失率{lr*100:.1f}%），"
                          f"现有实际毛利{am*100:.1f}%（{src}实测）——够了，不动价")
            else:
                tier = None
                for key, tm, cap in BUMP_TIERS:
                    if gap <= cap:
                        tier, target = key, tm
                        break
                if tier is None:
                    tier, target = "delist", None
                    reason = (f"退货损失率{lr*100:.1f}%（{src}），需要毛利{need*100:.1f}%，"
                              f"实际只有{am*100:.1f}%、缺{gap*100:.1f}个点——升到18%档也补不齐，建议下架")
                else:
                    reason = (f"需要毛利{need*100:.1f}%（10%+退货损失率{lr*100:.1f}%），"
                              f"实际毛利{am*100:.1f}%（{src}）、缺{gap*100:.1f}个点——"
                              f"升到名义{int(target*100)}%档补上（实际毛利约+{gap*100:.1f}~{(gap+0.01)*100:.0f}点）")

            counts[tier] = counts.get(tier, 0) + 1
            rows.append((store_key, sku, tier, target, reason,
                         json.dumps(ev, ensure_ascii=False), 0,
                         orders90, returns90, round(am, 4), None,
                         listed_days, cur_price or None, cost or None,
                         round(lr, 4), src, op, now))

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

# 单步改价安全阀：一次最多提15%、最多降20%，防一步跳太猛（被夹住的下轮再走一步）
MAX_STEP_UP = 0.15
MAX_STEP_DOWN = 0.20
MIN_DEVIATION = 0.01     # 目标价和现价差1%以内不折腾
COLD_BATCH = 500   # 零销量降档促活每轮最多放500个候选（分批观察激活率，防一次全动）

ACTION_TIERS = ("tier_15", "tier_18", "cold_12")   # 有价格动作的档位（keep=不动价）


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
        target_discount = round(bd.discount_price, 2)
        dev = (target_discount - cur_discount) / cur_discount
        clamp_note = ""
        if dev > MAX_STEP_UP:
            target_discount = round(cur_discount * (1 + MAX_STEP_UP), 2)
            clamp_note = f"（单步限+{MAX_STEP_UP:.0%}，剩余下轮再走）"
            summary["clamped"] += 1
        elif dev < -MAX_STEP_DOWN:
            target_discount = round(cur_discount * (1 - MAX_STEP_DOWN), 2)
            clamp_note = f"（单步限−{MAX_STEP_DOWN:.0%}）"
            summary["clamped"] += 1
        dev = (target_discount - cur_discount) / cur_discount
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
