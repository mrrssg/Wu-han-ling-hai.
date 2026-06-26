"""
成交价对账哨兵 (Order-price Guard) — Part 1 monitor's safety net.

WHY THIS EXISTS
---------------
The daily repricing monitor (repricing_monitor_service) evaluates profit against
the *offer-price snapshot* in offerprice_listing, which is refreshed only once a
day by the 02:20 OF52 cron. A price that goes rogue *intraday* — gets mis-set,
racks up orders, then is reverted before the next snapshot — is completely
invisible to it. The monitor also never looks at what actually SOLD.

Incident (ATCO-MDLW276312, lowes_autool, 2026-06-23): real sale price $94.98 vs
supplier cost $217.49 → ~$137 loss/unit on 7 ordered units, while the monitor
saw the stale $633.96/$316.98 snapshot and skipped it every day.

WHAT THIS DOES
--------------
Runs frequently (cron, hourly). For every recent, non-cancelled order line it
computes the REAL margin from the ACTUAL sale price (order_data.price_unit) and
the LATEST supplier cost. Anything below threshold is recorded in
order_system.order_guard_alert and pushed to Feishu once. Because it keys off
real orders, a stale snapshot cannot hide a loss from it.

SCOPE: all 4 active Mirakl stores (macy_kuyotq, macy_wopet, lowes_autool,
lowes_yasonic). Stores without a Feishu pricing config (wopet/yasonic) still get
a loss check — commission comes from the order line's own commission_fee and the
supplier cost from the supplier tables; only the return-shipping component is
omitted (it would only make the margin look *better*, so we never miss a loss).

ALERT-ONLY: this module performs NO Mirakl write. It records + notifies; a human
fixes the price from the /repricing dashboard. (Per user decision 2026-06-26.)

This module makes NO Mirakl API call. It reads autoweb DB + posts to a Feishu
group bot webhook.
"""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import current_app

from app.models.db_manager import DBManager
from app.services.repricing_monitor_service import _to_float
from app.services.repricing_formula import cost_from_supplier_price, return_shipping_total
from app.services.repricing_stores import REPRICING_STORES, get_store, all_store_keys


_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# ---------- thresholds ----------
LOW_MARGIN_THRESHOLD = 0.05      # margin below this -> alert (matches the monitor)
# margin < 0            -> severity 'loss'   (urgent, selling below cost)
# 0 <= margin < 0.05    -> severity 'low_margin'

DEFAULT_LOOKBACK_HOURS = 48      # generous window; dedup stops repeat notifications
DEFAULT_COMMISSION_FALLBACK = 0.15   # only used when a line has no commission_fee
RETURN_COST_RATIO = 0.10         # 退货成本预估 = 退货运费(加附加费) * 0.10

# order states that represent a real, committed sale (NOT cancelled/refunded)
_EXCLUDED_STATES = ("CANCELED", "CANCELLED", "REFUSED", "REFUNDED", "REJECTED")

_ORDER_TABLE = {
    "Macy": "order_system.macy_order_data",
    "Lowes": "order_system.lowes_order_data",
    "BestBuy": "order_system.bestbuy_order_data",
}


# =============================================================================
# Cost resolution (works for every store, with or without a Feishu config)
# =============================================================================

def load_supplier_prices() -> Dict[str, Dict[str, float]]:
    """Preload {SKU: Price} for Costway + Vevor in ONE pass each.

    The guard evaluates hundreds of order lines per run; looking each cost up
    with its own DB round-trip is the N+1 trap that made the old monitor crawl
    (经验教训 #12). Load both supplier tables into memory once and probe the
    dicts instead.
    """
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT SKU, Price FROM autooperate.newestdropship")
            costway = {r["SKU"]: _to_float(r["Price"]) for r in (cursor.fetchall() or [])}
            cursor.execute("SELECT SKU, Price FROM autooperate.newestdropship_vevor")
            vevor = {r["SKU"]: _to_float(r["Price"]) for r in (cursor.fetchall() or [])}
    finally:
        conn.close()
    return {"Costway": costway, "Vevor": vevor}


def resolve_cost(warehouse_sku: str, supplier_hint: Optional[str],
                 price_maps: Dict[str, Dict[str, float]]
                 ) -> Tuple[Optional[str], Optional[float], Optional[float]]:
    """Return (supplier, supplier_price, unit_cost) for a warehouse_sku using
    preloaded price maps.

    Prefer the configured supplier; otherwise probe Costway then Vevor. Only
    Costway/Vevor have a known cost factor; anything else returns (None,...)
    and the caller skips the line (no false loss alert).
    """
    if supplier_hint in ("Costway", "Vevor"):
        candidates = [supplier_hint]
    else:
        candidates = ["Costway", "Vevor"]
    for sup in candidates:
        price = price_maps.get(sup, {}).get(warehouse_sku)
        if price is not None:
            return sup, price, cost_from_supplier_price(price, sup)
    return None, None, None


# =============================================================================
# Per-store config + offer snapshot maps
# =============================================================================

def _fetch_config_map(store_key: str) -> Dict[str, Dict]:
    """{warehouse_sku: config_row} for the store (may be empty for
    offer_sync_only stores)."""
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """SELECT warehouse_sku, supplier, discount_factor, commission_rate,
                          return_shipping_base, length_in, width_in, height_in, weight_lb
                     FROM order_system.offer_pricing_config
                    WHERE store_key=%s""",
                (store_key,),
            )
            rows = cursor.fetchall() or []
    finally:
        conn.close()
    return {r["warehouse_sku"]: r for r in rows}


def _fetch_recent_orders(platform: str, shop_name: str, cutoff: datetime) -> List[Dict]:
    """Recent, non-cancelled order lines for the store. JOIN to offerprice_listing
    scopes naturally to the store (shop_sku prefixes are store-unique) and yields
    the warehouse_sku in one shot."""
    table = _ORDER_TABLE.get(platform)
    if not table:
        return []
    excl = ",".join(["%s"] * len(_EXCLUDED_STATES))
    sql = f"""
        SELECT o.order_id, o.order_line_id, o.created_date, o.order_state,
               o.offer_sku, o.price_unit, o.quantity, o.commission_fee,
               o.total_order_amount,
               l.warehouse_sku, l.origin_price, l.discount_price
          FROM {table} o
          JOIN order_system.offerprice_listing l
            ON l.shop_sku = o.offer_sku AND l.platform=%s AND l.shop_name=%s
         WHERE o.created_date >= %s
           AND UPPER(o.order_state) NOT IN ({excl})
    """
    params = (platform, shop_name, cutoff) + _EXCLUDED_STATES
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall() or []
    finally:
        conn.close()


# =============================================================================
# Margin math (uses the REAL sale price + REAL commission from the order line)
# =============================================================================

def _effective_discount_factor(store_key: str, cfg: Optional[Dict]) -> Optional[float]:
    """Store-level override wins, else per-SKU Feishu discount_factor."""
    scfg = get_store(store_key)
    override = scfg.get("discount_factor_override")
    if override is not None:
        return float(override)
    if cfg is not None:
        return _to_float(cfg.get("discount_factor"))
    return None


def _evaluate_line(store_key: str, row: Dict, cfg: Optional[Dict],
                   price_maps: Dict[str, Dict[str, float]]) -> Optional[Dict]:
    """Return an alert dict if this order line is below the margin threshold,
    else None (healthy / unevaluable)."""
    wh = row.get("warehouse_sku")
    if not wh:
        return None

    qty = int(row.get("quantity") or 1) or 1
    sale_unit = _to_float(row.get("price_unit"))
    if sale_unit is None or sale_unit <= 0:
        return None
    revenue = sale_unit * qty

    supplier = cfg.get("supplier") if cfg else None
    supplier, supplier_price, unit_cost = resolve_cost(wh, supplier, price_maps)
    if unit_cost is None:
        return None  # unknown supplier/cost -> cannot judge, skip (no false alert)
    cost = unit_cost * qty

    # real commission from the order line; fall back to a rate only if absent
    commission = _to_float(row.get("commission_fee"))
    if commission is None:
        rate = (_to_float(cfg.get("commission_rate")) if cfg else None) or DEFAULT_COMMISSION_FALLBACK
        commission = revenue * rate

    # return-cost estimate only when we have dims + base (configured stores)
    return_cost_est = 0.0
    if cfg is not None:
        base = _to_float(cfg.get("return_shipping_base"))
        L = _to_float(cfg.get("length_in"))
        W = _to_float(cfg.get("width_in"))
        H = _to_float(cfg.get("height_in"))
        wt = _to_float(cfg.get("weight_lb"))
        if base is not None and None not in (L, W, H, wt) and all(v > 0 for v in (L, W, H)):
            rs_total = return_shipping_total(base, L, W, H, wt)
            return_cost_est = rs_total * RETURN_COST_RATIO * qty

    profit = revenue - commission - cost - return_cost_est
    margin = profit / revenue if revenue else 0.0
    if margin >= LOW_MARGIN_THRESHOLD:
        return None

    # expected (intended) per-unit price, for context in the alert
    eff_df = _effective_discount_factor(store_key, cfg)
    origin = _to_float(row.get("origin_price"))
    disc = _to_float(row.get("discount_price"))
    if disc is not None:
        expected = disc
    elif origin is not None and eff_df:
        expected = origin * eff_df
    else:
        expected = origin

    return {
        "order_id": row.get("order_id"),
        "order_line_id": row.get("order_line_id"),
        "order_created": row.get("created_date"),
        "order_state": row.get("order_state"),
        "shop_sku": row.get("offer_sku"),
        "warehouse_sku": wh,
        "supplier": supplier,
        "supplier_price": round(supplier_price, 2) if supplier_price is not None else None,
        "unit_cost": round(unit_cost, 2),
        "sale_price_unit": round(sale_unit, 2),
        "expected_price": round(expected, 2) if expected is not None else None,
        "quantity": qty,
        "commission_fee": round(commission, 2),
        "return_cost_est": round(return_cost_est, 2),
        "line_revenue": round(revenue, 2),
        "line_profit": round(profit, 2),
        "margin": round(margin, 4),
        "severity": "loss" if margin < 0 else "low_margin",
    }


# =============================================================================
# Persist + notify
# =============================================================================

def _upsert_alerts(store_key: str, platform: str, shop_name: str,
                   alerts: List[Dict]) -> None:
    if not alerts:
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            for a in alerts:
                cursor.execute(
                    """
                    INSERT INTO order_system.order_guard_alert
                        (store_key, platform, shop_name, order_id, order_line_id,
                         order_created, order_state, shop_sku, warehouse_sku,
                         supplier, supplier_price, unit_cost, sale_price_unit,
                         expected_price, quantity, commission_fee, return_cost_est,
                         line_revenue, line_profit, margin, severity, detected_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE
                        order_state    = VALUES(order_state),
                        supplier       = VALUES(supplier),
                        supplier_price = VALUES(supplier_price),
                        unit_cost      = VALUES(unit_cost),
                        sale_price_unit= VALUES(sale_price_unit),
                        expected_price = VALUES(expected_price),
                        commission_fee = VALUES(commission_fee),
                        return_cost_est= VALUES(return_cost_est),
                        line_revenue   = VALUES(line_revenue),
                        line_profit    = VALUES(line_profit),
                        margin         = VALUES(margin),
                        severity       = VALUES(severity)
                    """,
                    (store_key, platform, shop_name, a["order_id"], a["order_line_id"],
                     a["order_created"], a["order_state"], a["shop_sku"], a["warehouse_sku"],
                     a["supplier"], a["supplier_price"], a["unit_cost"], a["sale_price_unit"],
                     a["expected_price"], a["quantity"], a["commission_fee"], a["return_cost_est"],
                     a["line_revenue"], a["line_profit"], a["margin"], a["severity"], now),
                )
        conn.commit()
    finally:
        conn.close()


def _fetch_unnotified(store_key: str) -> List[Dict]:
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """SELECT id, shop_sku, warehouse_sku, supplier, order_id, order_state,
                          sale_price_unit, expected_price, unit_cost, quantity,
                          line_profit, margin, severity
                     FROM order_system.order_guard_alert
                    WHERE store_key=%s AND notified_at IS NULL AND resolved_at IS NULL
                    ORDER BY (severity='loss') DESC, margin ASC""",
                (store_key,),
            )
            return cursor.fetchall() or []
    finally:
        conn.close()


def _mark_notified(ids: List[int]) -> None:
    if not ids:
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    placeholders = ",".join(["%s"] * len(ids))
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                f"UPDATE order_system.order_guard_alert SET notified_at=%s "
                f"WHERE id IN ({placeholders})",
                tuple([now] + ids),
            )
        conn.commit()
    finally:
        conn.close()


# =============================================================================
# Feishu group-bot webhook push
# =============================================================================

def _webhook_url() -> Optional[str]:
    """Read the Feishu custom-bot webhook from instance/repricing/alert_webhook.txt.
    Absent file -> push is skipped (alerts still recorded + shown on dashboard)."""
    path = _PROJECT_ROOT / "instance" / "repricing" / "alert_webhook.txt"
    try:
        if path.exists():
            url = path.read_text(encoding="utf-8").strip()
            return url or None
    except OSError:
        pass
    return None


def _build_message(store_label: str, rows: List[Dict]) -> str:
    losses = [r for r in rows if r["severity"] == "loss"]
    lows = [r for r in rows if r["severity"] != "loss"]
    # keyword "价格预警" lets a keyword-restricted bot accept the message
    lines = [f"🚨 价格预警 — {store_label}",
             f"亏本 {len(losses)} 条，低利润(<5%) {len(lows)} 条"]
    shown = (losses + lows)[:20]
    for r in shown:
        flag = "🔴亏本" if r["severity"] == "loss" else "🟡低利润"
        m = r.get("margin")
        m_txt = f"{float(m) * 100:.1f}%" if m is not None else "-"
        lines.append(
            f"{flag} {r['shop_sku']} | 售${r['sale_price_unit']} vs 应售${r['expected_price']} "
            f"| 成本${r['unit_cost']} | 利润率{m_txt} | 单{r['order_id']}({r['order_state']})"
        )
    if len(rows) > len(shown):
        lines.append(f"… 其余 {len(rows) - len(shown)} 条见 /repricing/loss-alerts")
    lines.append("请尽快到 Mirakl 后台核对/改价。")
    return "\n".join(lines)


def _push_feishu(text: str) -> Dict[str, Any]:
    url = _webhook_url()
    if not url:
        return {"pushed": False, "reason": "no webhook configured"}
    try:
        r = requests.post(
            url, json={"msg_type": "text", "content": {"text": text}}, timeout=15
        )
        ok = False
        try:
            ok = r.json().get("StatusCode") == 0 or r.json().get("code") == 0
        except ValueError:
            ok = r.status_code == 200
        return {"pushed": ok, "http_status": r.status_code, "body": r.text[:300]}
    except requests.RequestException as exc:
        return {"pushed": False, "reason": str(exc)}


# =============================================================================
# Public entry points
# =============================================================================

def run_order_guard(store_key: str, lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
                    notify: bool = True,
                    price_maps: Optional[Dict[str, Dict[str, float]]] = None
                    ) -> Dict[str, Any]:
    """Scan recent orders for one store, record below-threshold lines, notify.

    price_maps: pass a preloaded supplier-price map to avoid reloading the
    supplier tables per store (run_all does this); None -> load here.
    """
    scfg = get_store(store_key)
    platform = scfg["platform"]
    shop_name = scfg["shop_name"]
    cutoff = datetime.now() - timedelta(hours=lookback_hours)

    config_map = _fetch_config_map(store_key)
    if price_maps is None:
        price_maps = load_supplier_prices()
    orders = _fetch_recent_orders(platform, shop_name, cutoff)

    alerts: List[Dict] = []
    for row in orders:
        cfg = config_map.get(row.get("warehouse_sku"))
        a = _evaluate_line(store_key, row, cfg, price_maps)
        if a is not None:
            alerts.append(a)

    _upsert_alerts(store_key, platform, shop_name, alerts)

    push_result = {"pushed": False, "reason": "notify disabled"}
    notified = 0
    if notify:
        fresh = _fetch_unnotified(store_key)
        if fresh:
            push_result = _push_feishu(_build_message(scfg["label"], fresh))
            if push_result.get("pushed"):
                _mark_notified([r["id"] for r in fresh])
                notified = len(fresh)

    loss_n = sum(1 for a in alerts if a["severity"] == "loss")
    return {
        "success": True,
        "store_key": store_key,
        "orders_scanned": len(orders),
        "below_threshold": len(alerts),
        "loss": loss_n,
        "low_margin": len(alerts) - loss_n,
        "notified": notified,
        "push": push_result,
        "lookback_hours": lookback_hours,
    }


def run_all(lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
            notify: bool = True) -> List[Dict[str, Any]]:
    """Run the guard for every active Mirakl store (supplier tables loaded once)."""
    price_maps = load_supplier_prices()
    out = []
    for sk in all_store_keys():
        try:
            out.append(run_order_guard(sk, lookback_hours=lookback_hours,
                                       notify=notify, price_maps=price_maps))
        except Exception as exc:  # one store failing must not kill the rest
            out.append({"success": False, "store_key": sk, "error": str(exc)})
    return out
