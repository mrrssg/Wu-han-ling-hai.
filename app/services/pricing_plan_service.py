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

BASELINE = 0.10            # 考核基线
STD_MARGIN = 0.12          # 标准档目标毛利
RISK_MARGIN = 0.15         # 风险档目标毛利
RISK_RATIO = 5.0           # 单退损失/单件利润 超过此倍数进风险档
REPAIR_TOL = 0.01          # 实际毛利低于目标超过1个百分点才算漏
MIN_ORDERS_SIGNAL = 3      # 判修复/风险至少要的单数（样本太小不动）
DELIST_RETURNS = 2         # 下架档：退货≥2
DELIST_NET = -50           # 且净贡献 < -50
COLD_WATCH_DAYS = 60       # 上架不足60天零销量=观察
COLD_PROBE_MARGIN = 0.10   # 冷启动首轮试探毛利

STORE_MAP = {  # store_key -> (platform, shop_name, profit_sku_90d.store)
    "lowes_autool": ("Lowes", "autool", "Lowes-Autool"),
}


def _qall(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params) if params else cur.execute(sql)
        return cur.fetchall() or []


def evaluate_store(store_key: str) -> Dict[str, Any]:
    platform, shop_name, profit_store = STORE_MAP[store_key]
    now = datetime.now(CN_TZ)
    conn = DBManager.get_connection()
    try:
        offers = _qall(conn, """
            SELECT shop_sku, title, category, cost_price, last_cost_snapshot,
                   price, origin_price, discount_price, listed_at
            FROM order_system.offerprice_listing
            WHERE platform=%s AND shop_name=%s AND active=1""", (platform, shop_name))
        sku_stats = {r["shop_sku"]: r for r in _qall(conn, """
            SELECT shop_sku, orders, sale, profit_gross, returns_cnt, loss_expected,
                   net, margin
            FROM order_system.profit_sku_90d WHERE store=%s""", (profit_store,))}
        # 海外仓退回记录（有跟踪号=货回来只亏运费，降低该SKU的单退损失）
        wh_skus = {r["shop_sku"]: int(r["n"]) for r in _qall(conn, """
            SELECT shop_sku, COUNT(*) AS n FROM order_system.return_case
            WHERE store=%s AND state='warehouse' GROUP BY shop_sku""", (profit_store,))}
        # 账本口径未定案的订单涉及的SKU（not_charged退货申请单，实际毛利可能被污染）
        suspect_skus = {r["shop_sku"] for r in _qall(conn, """
            SELECT DISTINCT shop_sku FROM order_system.return_case
            WHERE store=%s AND state='not_charged'""", (profit_store,))}

        rows = []
        counts: Dict[str, int] = {}
        for o in offers:
            sku = o["shop_sku"]
            s = sku_stats.get(sku)
            cost = float(o["cost_price"] or o["last_cost_snapshot"] or 0)
            cur_price = float(o["discount_price"] or o["price"] or o["origin_price"] or 0)
            listed_days = (now.date() - o["listed_at"].date()).days if o["listed_at"] else None

            orders = int(s["orders"] or 0) if s else 0
            returns = int(s["returns_cnt"] or 0) if s else 0
            margin = float(s["margin"]) if s and s["margin"] is not None else None
            sale = float(s["sale"] or 0) if s else 0.0
            gross = float(s["profit_gross"] or 0) if s else 0.0
            net = float(s["net"] or 0) if s else 0.0
            nonret = max(orders - returns, 0)
            gm = (gross / sale) if sale > 0 else None
            unit_profit = (gross / nonret) if nonret > 0 else None
            wh_n = wh_skus.get(sku, 0)
            # 单退损失：没跟踪号亏货值；该SKU有海外仓退回记录则按只亏运费(售价10%)估
            loss_single = (0.10 * cur_price) if (wh_n > 0 and cur_price > 0) else cost
            ratio = (loss_single / unit_profit) if (unit_profit and unit_profit > 0) else None

            ev = {"orders_90d": orders, "returns_90d": returns,
                  "sale_90d": round(sale, 2), "net_90d": round(net, 2),
                  "gross_margin": round(gm, 4) if gm is not None else None,
                  "unit_profit": round(unit_profit, 2) if unit_profit else None,
                  "single_return_loss": round(loss_single, 2),
                  "loss_ratio": round(ratio, 1) if ratio else None,
                  "warehouse_returns": wh_n, "listed_days": listed_days,
                  "cur_price": cur_price, "cost": cost}

            if orders == 0:
                if listed_days is not None and listed_days < COLD_WATCH_DAYS:
                    tier, target = "cold_watch", None
                    reason = (f"上架{listed_days}天还没出单——新品有流量爬坡期，"
                              f"先观察到{COLD_WATCH_DAYS}天，不动价")
                else:
                    tier, target = "cold_probe", COLD_PROBE_MARGIN
                    d = f"{listed_days}天" if listed_days is not None else "很久(无上架时间)"
                    reason = (f"上架{d}零销量——没有利润可保，降价买信息：目标毛利降到10%试探，"
                              f"出了第一单就转正常评档；试探价也不能亏本卖")
            elif returns >= DELIST_RETURNS and net < DELIST_NET:
                tier, target = "delist", None
                reason = (f"90天退货{returns}次、净贡献−${-net:,.0f}——继续卖一单多亏一单，"
                          f"提价救不了，建议下架止血")
            elif (ratio is not None and ratio > RISK_RATIO and orders >= MIN_ORDERS_SIGNAL):
                tier, target = "risk", RISK_MARGIN
                how = "货回海外仓只亏运费" if wh_n else "货值(退货回不来时)"
                reason = (f"退一单亏${loss_single:,.0f}（{how}），一单只赚${unit_profit:,.0f}"
                          f"——{ratio:.0f}倍，12%的毛利兜不住退货风险，目标提到15%，"
                          f"多的3个点是退货保险")
            elif (gm is not None and gm < STD_MARGIN - REPAIR_TOL
                  and orders >= MIN_ORDERS_SIGNAL):
                tier, target = "repair", STD_MARGIN
                reason = (f"按12%定的价，实际卖出毛利只有{gm*100:.1f}%（{orders}单证据）"
                          f"——成本涨了价没跟/折扣打穿/定价错误，逐单核实后修回12%")
            else:
                tier, target = "standard", STD_MARGIN
                if gm is not None:
                    reason = (f"实际毛利{gm*100:.1f}%达标、退货风险正常（{orders}单）"
                              f"——卖得好的不动，保持12%")
                else:
                    reason = f"有{orders}单但样本太小，按标准档12%持有，攒数据再评"

            counts[tier] = counts.get(tier, 0) + 1
            rows.append((store_key, sku, tier, target, reason,
                         json.dumps(ev, ensure_ascii=False),
                         1 if sku in suspect_skus else 0,
                         orders, returns, margin, gm, listed_days,
                         cur_price or None, cost or None, now))

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

ACTION_TIERS = ("risk", "repair", "cold_probe")   # 有价格动作的档位


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
