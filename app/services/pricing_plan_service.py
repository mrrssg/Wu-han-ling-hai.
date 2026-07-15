# -*- coding: utf-8 -*-
"""分档定价评档（Lowes-Autool 起步）。

用户定案的五档（2026-07-15/16）：
  standard   标准档 12%：退货风险正常，已达标的一律不动（对好SKU公平）
  risk       风险档 15%：退一单亏的钱 > 5倍单件利润——12%兜不住退货，多3个点是保险
  repair     修复档：本来按12%定价，实际卖出毛利掉下去了（成本涨/折扣打穿）→ 核实后修回
  delist     下架档：退货≥2且净亏——提价救不了，止血
  cold_watch / cold_probe  冷启动：在卖但零销量。<60天先观察；≥60天降价试探（10%），
             目标是"出第一单买信息"，出单即转正常评档。零销量没有利润可保，降价风险≈0。

铁律：
  * 全集 = offerprice_listing active=1（数据库offer=在卖；飞书只是资料，用户2026-07-16定）
  * 评档只写 pricing_tier 表 + 原因 + 证据，绝不直接改价——改价必须人工确认走待改价管道
  * 重评 UPSERT 不覆盖人工 status
"""
import json
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List

from app.models.db_manager import DBManager

CN_TZ = timezone(timedelta(hours=8))

BASELINE = 0.10            # 考核基线（唯一不动的常数）
LGD_DEFAULT = 0.72         # 退一单损失占售价比兜底（铺货模式按货值算，用户2026-07-16定案）
K_SHRINK = 8               # 收缩权重：SKU退货率=(退货数+K×类目率)/(订单数+K)，防1单1退=100%
COLD_WATCH_DAYS = 60       # 零销量<60天=新品观察（60天后才开张的历史占比仅7%）

# 固定四档（用户2026-07-16定：不要每SKU一个利润率，就分几档；最低12%最高18%）
# 上限逻辑：该档毛利−预期退货税≥10%基线 → r̂×LGD ≤ 档位−10%
TIERS = [
    ("tier_12", 0.12),     # 卖得好退货低（预期退货税≤2%）
    ("tier_15", 0.15),     # 默认/新品档
    ("tier_18", 0.18),     # 高退货档
]

STORE_MAP = {  # store_key -> (platform, shop_name, mirakl shop_id)
    "lowes_autool": ("Lowes", "autool", 10),
}


def _qall(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params) if params else cur.execute(sql)
        return cur.fetchall() or []


def evaluate_store(store_key: str) -> Dict[str, Any]:
    """固定四档评档（订单/退货全部数据库直读，2026-07-16定案）：
    m_need = 10%基线 + 预期退货率×LGD(货值/售价) → 向上取最近档(12/15/18)，
    >18%盖不住 → 下架档；零销量：<60天观察，≥60天降到12%档促活（最低就是12%）。
    预期退货率 = (SKU退货数 + K×类目退货率) ÷ (SKU订单数 + K) —— 群体收缩，
    1单1退不会算成100%，"卖得好退货低"的SKU才收缩得出≤2%的低税率进12档。"""
    platform, shop_name, shop_id = STORE_MAP[store_key]
    now = datetime.now(CN_TZ)
    conn = DBManager.get_connection()
    try:
        offers = _qall(conn, """
            SELECT shop_sku, category, cost_price, last_cost_snapshot,
                   price, origin_price, discount_price, listed_at
            FROM order_system.offerprice_listing
            WHERE platform=%s AND shop_name=%s AND active=1""", (platform, shop_name))
        # 订单（数据库直读，90天，剔除取消）
        sku_orders = {r["offer_sku"]: r for r in _qall(conn, """
            SELECT offer_sku, COUNT(DISTINCT order_id) AS orders,
                   ROUND(SUM(line_total_price),2) AS sale
            FROM order_system.lowes_order_data
            WHERE shop_id=%s AND order_state<>'CANCELED'
              AND created_date >= DATE_SUB(CURDATE(), INTERVAL 90 DAY)
            GROUP BY offer_sku""", (shop_id,))}
        # 退货（mirakl_returns直读，90天，按订单关联到SKU）
        sku_returns = {r["offer_sku"]: int(r["rets"]) for r in _qall(conn, """
            SELECT d.offer_sku, COUNT(DISTINCT r.order_id) AS rets
            FROM order_system.mirakl_returns r
            JOIN order_system.lowes_order_data d
              ON d.order_id = r.order_id AND d.shop_id = %s
            WHERE r.platform=%s AND r.shop_name=%s
              AND r.date_created >= DATE_SUB(CURDATE(), INTERVAL 90 DAY)
            GROUP BY d.offer_sku""", (shop_id, platform, shop_name))}

        # 类目退货率（收缩的群体先验）+ 全店率兜底
        cat_orders: Dict[str, int] = {}
        cat_rets: Dict[str, int] = {}
        offer_cat = {o["shop_sku"]: (o["category"] or "(无类目)") for o in offers}
        for sku, st in sku_orders.items():
            cat = offer_cat.get(sku, "(无类目)")
            cat_orders[cat] = cat_orders.get(cat, 0) + int(st["orders"] or 0)
            cat_rets[cat] = cat_rets.get(cat, 0) + sku_returns.get(sku, 0)
        store_orders = sum(cat_orders.values()) or 1
        store_rate = sum(cat_rets.values()) / store_orders

        def cat_rate(cat: str) -> float:
            n = cat_orders.get(cat, 0)
            if n >= 30:
                return cat_rets.get(cat, 0) / n
            return store_rate

        rows = []
        counts: Dict[str, int] = {}
        for o in offers:
            sku = o["shop_sku"]
            cat = offer_cat[sku]
            st = sku_orders.get(sku)
            orders = int(st["orders"] or 0) if st else 0
            sale = float(st["sale"] or 0) if st else 0.0
            returns = sku_returns.get(sku, 0)
            cost = float(o["cost_price"] or o["last_cost_snapshot"] or 0)
            cur_price = float(o["discount_price"] or o["price"] or o["origin_price"] or 0)
            listed_days = (now.date() - o["listed_at"].date()).days if o["listed_at"] else None

            prior = cat_rate(cat)
            r_hat = (returns + K_SHRINK * prior) / (orders + K_SHRINK)
            lgd = (cost / cur_price) if (cost > 0 and cur_price > 0) else LGD_DEFAULT
            lgd = min(lgd, 0.95)
            tax = r_hat * lgd
            m_need = BASELINE + tax

            ev = {"orders_90d": orders, "returns_90d": returns, "sale_90d": round(sale, 2),
                  "raw_return_rate": round(returns / orders, 4) if orders else None,
                  "cat": cat, "cat_rate": round(prior, 4),
                  "r_hat": round(r_hat, 4), "lgd": round(lgd, 4),
                  "return_tax": round(tax, 4), "m_need": round(m_need, 4),
                  "listed_days": listed_days, "cur_price": cur_price, "cost": cost}

            if orders == 0:
                if listed_days is not None and listed_days < COLD_WATCH_DAYS:
                    tier, target = "cold_watch", None
                    reason = (f"上架{listed_days}天还没出单——新品观察期（60天内），不动价")
                else:
                    tier, target = "cold_12", 0.12
                    d = f"{listed_days}天" if listed_days is not None else "60天以上(老批次)"
                    reason = (f"上架{d}零销量——降到最低档12%促活（最低就到12%不再往下）；"
                              f"出了第一单转正常评档")
            elif m_need > TIERS[-1][1]:
                tier, target = "delist", None
                reason = (f"预期退货率{r_hat*100:.1f}%（{orders}单{returns}退，类目率{prior*100:.1f}%）"
                          f"×货值占比{lgd*100:.0f}% = 退货税{tax*100:.1f}个点，"
                          f"18%毛利都盖不住10%基线——建议下架止血")
            else:
                for key, tm in TIERS:
                    if tm >= m_need:
                        tier, target = key, tm
                        break
                if tier == "tier_12":
                    reason = (f"卖得好退货低：{orders}单仅{returns}退，预期退货率{r_hat*100:.1f}%"
                              f"（含类目先验修正），退货税{tax*100:.1f}个点——12%毛利即可稳过基线，"
                              f"给最低档保价格竞争力")
                elif tier == "tier_15":
                    reason = (f"预期退货率{r_hat*100:.1f}%（{orders}单{returns}退，类目率{prior*100:.1f}%），"
                              f"退货税{tax*100:.1f}个点——15%档=10%基线+退货税+余量")
                else:
                    reason = (f"退货偏高：预期退货率{r_hat*100:.1f}%（{orders}单{returns}退，"
                              f"类目率{prior*100:.1f}%），退货税{tax*100:.1f}个点——"
                              f"顶到18%档才盖得住基线")

            counts[tier] = counts.get(tier, 0) + 1
            rows.append((store_key, sku, tier, target, reason,
                         json.dumps(ev, ensure_ascii=False), 0,
                         orders, returns, None,
                         round(returns / orders, 4) if orders else None,
                         listed_days, cur_price or None, cost or None, now))

        sql = """
            INSERT INTO order_system.pricing_tier
                (store_key, shop_sku, tier, target_margin, reason_text, evidence_json,
                 data_suspect, orders_90d, returns_90d, margin_90d, gross_margin,
                 listed_days, cur_price, cost_price, assigned_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
                tier=VALUES(tier), target_margin=VALUES(target_margin),
                reason_text=VALUES(reason_text), evidence_json=VALUES(evidence_json),
                data_suspect=VALUES(data_suspect), orders_90d=VALUES(orders_90d),
                returns_90d=VALUES(returns_90d), margin_90d=VALUES(margin_90d),
                gross_margin=VALUES(gross_margin), listed_days=VALUES(listed_days),
                cur_price=VALUES(cur_price), cost_price=VALUES(cost_price),
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

ACTION_TIERS = ("tier_12", "tier_15", "tier_18", "cold_12")   # 有价格动作的档位


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
