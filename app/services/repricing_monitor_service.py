"""
Part 1 of the Macy-kuyotq automated repricing system.

Runs once a day after offer-listing-sync and feishu-pricing-config-sync.
For every active offer:

    1. Hard precheck: supplier-table freshness (Costway + Vevor MAX(Updated_At)
       within MAX_SUPPLIER_STALE_HOURS, otherwise the whole run aborts).
    2. Skip if SKU is blacklisted.
    3. Skip + alert if no Feishu config for the warehouse_sku.
    4. Skip + alert if Feishu config has no return_shipping_base.
    5. Skip + alert if no supplier cost row.
    6. Skip + alert if |new_cost - old_cost| / old_cost > COST_VOLATILITY (0.30).
    7. Compute realised margin (current DB origin_price + new cost +
       Feishu inputs). If margin >= PROFIT_THRESHOLD (0.05), skip.
    8. Compute target origin_price via the standard Feishu formula.
    9. Call OF24 (dry_run by default; production wiring lives in the cron).
   10. On 200/201: update offerprice_listing.origin_price + last_cost_snapshot,
       reset failure_count. Status = success.
       On non-2xx: increment failure_count; >= 3 -> blacklist.
   12. Every decision (including SKIPPED ones) gets a row in
       offer_price_change_log so the audit trail is comprehensive.

Stores: macy_kuyotq + lowes_autool (see repricing_stores.REPRICING_STORES).
"""
import hashlib
import json
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from app.models.db_manager import DBManager
from app.services.mirakl_offer_api_service import update_offers
from app.services.repricing_formula import (
    calculate_breakdown,
    cost_from_supplier_price,
    cost_volatility_exceeds,
    realised_margin,
    return_shipping_total,
)


# ---------- config (will be moved to instance/repricing_config.json later) ----------
PROFIT_THRESHOLD = 0.05                # 无档位记录SKU的兜底触发线（灾难水位）
TIER_MARGIN_SLACK = 0.02               # 分档联动：现毛利 < 档位目标−2点 → 修回档位公式价（2026-07-16定案）
COST_VOLATILITY_THRESHOLD = 0.30       # |new-old|/old above this -> alert, skip
MAX_SUPPLIER_STALE_HOURS = 36
MAX_FAILURES_BEFORE_BLACKLIST = 3
OF24_BATCH_SIZE = 50


# ---------- value object passed through the pipeline ----------
@dataclass
class OfferContext:
    shop_sku: str
    warehouse_sku: Optional[str]
    db_origin_price: Optional[float]
    db_discount_price: Optional[float]
    raw_json: Optional[str]
    state_code: Optional[str]
    quantity: Optional[int]
    last_cost_snapshot: Optional[float]


# =============================================================================
# Helpers - supplier freshness + cost lookup
# =============================================================================

def _to_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def get_supplier_freshness() -> Dict[str, Any]:
    """Return MAX(Updated_At) per supplier table and a stale flag."""
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT MAX(Updated_At) AS m FROM autooperate.newestdropship"
            )
            costway_max = (cursor.fetchone() or {}).get("m")
            cursor.execute(
                "SELECT MAX(Updated_At) AS m FROM autooperate.newestdropship_vevor"
            )
            vevor_max = (cursor.fetchone() or {}).get("m")
    finally:
        conn.close()

    now = datetime.now()
    threshold = now - timedelta(hours=MAX_SUPPLIER_STALE_HOURS)
    return {
        "costway_max": costway_max,
        "vevor_max": vevor_max,
        "costway_stale": costway_max is None or costway_max < threshold,
        "vevor_stale": vevor_max is None or vevor_max < threshold,
        "threshold_hours": MAX_SUPPLIER_STALE_HOURS,
    }


def lookup_supplier_price(warehouse_sku: str, supplier: str) -> Tuple[Optional[float], Optional[datetime]]:
    """Return (Price, Updated_At) from the correct supplier table."""
    table = {
        "Costway": "autooperate.newestdropship",
        "Vevor": "autooperate.newestdropship_vevor",
    }.get(supplier)
    if not table:
        return None, None
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                f"SELECT Price, Updated_At FROM {table} WHERE SKU=%s LIMIT 1",
                (warehouse_sku,),
            )
            row = cursor.fetchone()
            if not row:
                return None, None
            return _to_float(row.get("Price")), row.get("Updated_At")
    finally:
        conn.close()


def _quick_loss_check(ctx, supplier, commission_rate, discount_factor, tier_map):
    """配置不全(缺退货运费基数/尺寸)时的亏本兜底检查——保证"在售就被监测到价格"。

    口径全部沿用现成的：
    - 卖价 = 折后成交价：discount_price → 原价×discount_factor 兜底（不能用活动前原价）
    - 成本 = 供应商价 × 系数（cost_from_supplier_price；组合套装整串查得到真实价）
    - 毛利**忽略退货损失**（退货只会让利润更差，是保守下限，只会漏报不会误报）
    - 触发线 = 有 pricing_tier 行→档位目标−TIER_MARGIN_SLACK；无档位→PROFIT_THRESHOLD
    返回 (is_loss, margin, cost, selling, threshold, tier_name)；算不了→(None,...)。"""
    if supplier not in ("Costway", "Vevor") or commission_rate is None:
        return None, None, None, None, None, None
    sp, _ = lookup_supplier_price(ctx.warehouse_sku, supplier)
    if sp is None:
        return None, None, None, None, None, None
    cost = cost_from_supplier_price(sp, supplier)
    selling = ctx.db_discount_price
    if (not selling or selling <= 0) and ctx.db_origin_price and discount_factor:
        selling = ctx.db_origin_price * discount_factor
    if not selling or selling <= 0:
        return None, None, cost, None, None, None
    margin = (selling * (1.0 - commission_rate) - cost) / selling
    trow = tier_map.get(ctx.shop_sku)
    tname = trow.get("tier") if trow else None
    tt = _to_float(trow.get("target_margin")) if trow else None
    threshold = (tt - TIER_MARGIN_SLACK) if tt is not None else PROFIT_THRESHOLD
    return (margin < threshold), margin, cost, selling, threshold, tname


def _emit_loss_alert(ctx, store_key, run_id, summary, supplier, commission_rate,
                     discount_factor, qc, blocked_reason):
    """qc=_quick_loss_check 结果；发高优先级「亏本在售」告警(alert_type=loss_uncovered)。"""
    _is_loss, margin, cost, selling, threshold, tname = qc
    _save_alert(ctx.shop_sku, store_key, "loss_uncovered",
                f"亏本在售 margin={margin:.4f}<{threshold:.2%}({tname or '无档'}) "
                f"sell={selling:.2f} cost={cost:.2f}；{blocked_reason}")
    _log(store_key, run_id, "auto_monitor", ctx, {
        "status": "alert", "alert_type": "loss_uncovered",
        "decision_reason": (f"折后价{selling:.2f}−佣金{commission_rate:.0%}−成本{cost:.2f} "
                            f"毛利{margin:.2%} < 触发线{threshold:.2%}(忽略退货损失,保守下限)；{blocked_reason}"),
        "supplier": supplier, "new_cost": round(cost, 4),
        "profit_margin_before": round(margin, 4),
        "discount_factor": discount_factor, "commission_rate": commission_rate,
    })
    summary["alert_loss_uncovered"] = summary.get("alert_loss_uncovered", 0) + 1


# =============================================================================
# DB queries
# =============================================================================

def fetch_active_offers(store_key: str = "macy_kuyotq") -> List[OfferContext]:
    from app.services.repricing_stores import get_store
    scfg = get_store(store_key)
    platform = scfg["platform"]
    shop_name = scfg["shop_name"]
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """SELECT shop_sku, warehouse_sku, origin_price, discount_price,
                          raw_json, state_code, quantity, last_cost_snapshot
                     FROM order_system.offerprice_listing
                    WHERE platform=%s AND shop_name=%s AND active=1
                    ORDER BY shop_sku""",
                (platform, shop_name),
            )
            rows = cursor.fetchall() or []
    finally:
        conn.close()

    out = []
    for r in rows:
        out.append(OfferContext(
            shop_sku=r["shop_sku"],
            warehouse_sku=r.get("warehouse_sku"),
            db_origin_price=_to_float(r.get("origin_price")),
            db_discount_price=_to_float(r.get("discount_price")),
            raw_json=r.get("raw_json"),
            state_code=r.get("state_code"),
            quantity=r.get("quantity"),
            last_cost_snapshot=_to_float(r.get("last_cost_snapshot")),
        ))
    return out


def fetch_pricing_configs(store_key: str = "macy_kuyotq") -> Dict[str, Dict]:
    """Return {warehouse_sku: config_row} for fast lookup."""
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """SELECT warehouse_sku, supplier, discount_factor, commission_rate,
                          return_shipping_base, length_in, width_in, height_in, weight_lb,
                          cost_buffer
                     FROM order_system.offer_pricing_config
                    WHERE store_key=%s""",
                (store_key,),
            )
            rows = cursor.fetchall() or []
    finally:
        conn.close()
    return {r["warehouse_sku"]: r for r in rows}


def is_blacklisted(shop_sku: str) -> bool:
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT blacklisted FROM order_system.offer_alert_state WHERE shop_sku=%s",
                (shop_sku,),
            )
            row = cursor.fetchone()
            return bool(row and row.get("blacklisted"))
    finally:
        conn.close()


# =============================================================================
# Logging
# =============================================================================

def _hash_payload(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _insert_log(cursor, log_row: Dict[str, Any]) -> int:
    cols = list(log_row.keys())
    placeholders = ",".join(["%s"] * len(cols))
    col_expr = ",".join(f"`{c}`" for c in cols)
    cursor.execute(
        f"INSERT INTO order_system.offer_price_change_log ({col_expr}) VALUES ({placeholders})",
        tuple(log_row[c] for c in cols),
    )
    return cursor.lastrowid


def _log(store_key: str, run_id: str, run_type: str, ctx: OfferContext,
         decision: Dict[str, Any]):
    """Persist one decision (skipped/alert/pending_verify/failed/...) to
    offer_price_change_log. Every numeric field already filled by the caller.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = {
        "run_id": run_id,
        "run_type": run_type,
        "store_key": store_key,
        "shop_sku": ctx.shop_sku,
        "warehouse_sku": ctx.warehouse_sku,
        "triggered_at": now,
        "status": decision.get("status"),
        "decision_reason": decision.get("decision_reason"),
        "alert_type": decision.get("alert_type"),
        "error_message": decision.get("error_message"),

        "supplier": decision.get("supplier"),
        "supplier_price_db": decision.get("supplier_price_db"),
        "supplier_data_age_hours": decision.get("supplier_data_age_hours"),
        "costway_updated_at": decision.get("costway_updated_at"),
        "vevor_updated_at": decision.get("vevor_updated_at"),

        "old_origin_price": ctx.db_origin_price,
        "new_origin_price": decision.get("new_origin_price"),
        "old_discount_price": ctx.db_discount_price,
        "new_discount_price": decision.get("new_discount_price"),
        "old_cost": ctx.last_cost_snapshot,
        "new_cost": decision.get("new_cost"),
        "cost_change_pct": decision.get("cost_change_pct"),

        "discount_factor": decision.get("discount_factor"),
        "commission_rate": decision.get("commission_rate"),
        "return_shipping_base": decision.get("return_shipping_base"),
        "return_shipping_extra": decision.get("return_shipping_extra"),
        "return_cost_estimate": decision.get("return_cost_estimate"),
        "total_cost": decision.get("total_cost"),

        "profit_margin_before": decision.get("profit_margin_before"),
        "profit_margin_after": decision.get("profit_margin_after"),
        "formula_calc_price": decision.get("formula_calc_price"),
        "target_origin_price": decision.get("target_origin_price"),

        "mirakl_called": decision.get("mirakl_called"),
        "mirakl_import_id": decision.get("mirakl_import_id"),
        "mirakl_http_status": decision.get("mirakl_http_status"),
        "mirakl_response_body": decision.get("mirakl_response_body"),
        "mirakl_payload_hash": decision.get("mirakl_payload_hash"),
        "mirakl_request_payload": decision.get("mirakl_request_payload"),

        "ip_used": decision.get("ip_used"),
        "api_call_seq": decision.get("api_call_seq"),
    }
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            _insert_log(cursor, row)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _increment_failure(shop_sku: str, store_key: str, reason: str) -> int:
    """Bump failure_count by 1; auto-blacklist when it crosses the threshold.
    Returns the new count.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """INSERT INTO order_system.offer_alert_state
                       (shop_sku, store_key, failure_count, last_alert_type,
                        last_alert_message, last_alert_at)
                   VALUES (%s, %s, 1, 'api_failure', %s, %s)
                   ON DUPLICATE KEY UPDATE
                       failure_count = failure_count + 1,
                       last_alert_type = VALUES(last_alert_type),
                       last_alert_message = VALUES(last_alert_message),
                       last_alert_at = VALUES(last_alert_at),
                       resolved_at = NULL""",
                (shop_sku, store_key, reason, now),
            )
            cursor.execute(
                "SELECT failure_count FROM order_system.offer_alert_state WHERE shop_sku=%s",
                (shop_sku,),
            )
            count = (cursor.fetchone() or {}).get("failure_count") or 0
            if count >= MAX_FAILURES_BEFORE_BLACKLIST:
                cursor.execute(
                    """UPDATE order_system.offer_alert_state
                          SET blacklisted=1, blacklisted_at=%s, blacklisted_reason=%s
                        WHERE shop_sku=%s""",
                    (now, f"auto-blacklist after {count} failures: {reason}", shop_sku),
                )
        conn.commit()
        return int(count)
    finally:
        conn.close()


def _reset_failure(shop_sku: str):
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """UPDATE order_system.offer_alert_state
                      SET failure_count = 0, resolved_at = NOW()
                    WHERE shop_sku = %s""",
                (shop_sku,),
            )
        conn.commit()
    finally:
        conn.close()


def _save_alert(shop_sku: str, store_key: str, alert_type: str, message: str):
    """Upsert into offer_alert_state with the latest alert (no failure
    increment - this is the path for *info* alerts like missing config).
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """INSERT INTO order_system.offer_alert_state
                       (shop_sku, store_key, last_alert_type, last_alert_message,
                        last_alert_at)
                   VALUES (%s, %s, %s, %s, %s)
                   ON DUPLICATE KEY UPDATE
                       last_alert_type = VALUES(last_alert_type),
                       last_alert_message = VALUES(last_alert_message),
                       last_alert_at = VALUES(last_alert_at),
                       resolved_at = NULL""",
                (shop_sku, store_key, alert_type, message, now),
            )
        conn.commit()
    finally:
        conn.close()


def _update_cost_snapshot(shop_sku: str, new_cost: float):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """UPDATE order_system.offerprice_listing
                      SET last_cost_snapshot=%s, last_cost_snapshot_at=%s
                    WHERE shop_sku=%s""",
                (new_cost, now, shop_sku),
            )
        conn.commit()
    finally:
        conn.close()


def _update_origin_price(shop_sku: str, new_origin: float, new_discount: float,
                         new_cost: float):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """UPDATE order_system.offerprice_listing
                      SET origin_price=%s, discount_price=%s,
                          last_cost_snapshot=%s, last_cost_snapshot_at=%s
                    WHERE shop_sku=%s""",
                (new_origin, new_discount, new_cost, now, shop_sku),
            )
        conn.commit()
    finally:
        conn.close()


# =============================================================================
# OF24 payload reconstruction
#
# OF52 export gives us a minimal field set. For OF24 we deliberately send only
# the fields we know - product_id/product_id_type are optional at update; the
# update_delete='update' flag means Mirakl will only overwrite the fields we
# include (per their docs: "fields not sent are reset to default" - but for
# pricing updates the safest interpretation is "fields we don't send keep
# their stored state"). We will validate this assumption empirically on P8
# with one or two SKUs.
# =============================================================================

def build_of24_payload(ctx: OfferContext, new_origin: float,
                        new_discount: Optional[float],
                        mode: str = "non_dropship") -> Dict[str, Any]:
    """Build a minimal OF24 update payload.

    NOTE: this is the monitor's lightweight builder (raw_json only). The
    canonical push path is the web routes' OF21 + build_of24_payload_from_full_offer
    which preserves every field. This builder is mode-aware so the rare
    `--live` monitor run does not write the wrong column for Dropship stores.

    - non_dropship (Macy): `price` IS the customer price.
    - dropship (Lowes): `price` is the wholesale cost (keep raw value);
      the customer price goes into retail_prices[].unit_origin_price /
      unit_discount_price.
    """
    raw = {}
    if ctx.raw_json:
        try:
            raw = json.loads(ctx.raw_json)
        except (TypeError, ValueError):
            raw = {}

    offer: Dict[str, Any] = {
        "shop_sku": ctx.shop_sku,
        "state_code": ctx.state_code or raw.get("state_code") or "11",
        "update_delete": "update",
        "quantity": ctx.quantity if ctx.quantity is not None else raw.get("quantity") or 0,
        "leadtime_to_ship": raw.get("leadtime_to_ship"),
        "logistic_class": (raw.get("logistic_class") or {}).get("code")
            if isinstance(raw.get("logistic_class"), dict)
            else raw.get("logistic_class"),
    }

    if mode == "dropship":
        # keep wholesale price as-is, change the retail side
        if raw.get("price") is not None:
            offer["price"] = round(float(raw["price"]), 2)
        retail_entry = {
            "channel_code": None,
            "unit_origin_price": round(float(new_origin), 2),
        }
        if new_discount is not None:
            retail_entry["unit_discount_price"] = round(float(new_discount), 2)
        offer["retail_prices"] = [retail_entry]
    else:
        offer["price"] = round(float(new_origin), 2)

    # drop keys whose value is None to avoid Mirakl rejecting nulls
    offer = {k: v for k, v in offer.items() if v is not None}
    return offer


# =============================================================================
# Main run
# =============================================================================

def run_monitor(store_key: str = "macy_kuyotq", dry_run: bool = True) -> Dict[str, Any]:
    """Top-level entry. Returns summary stats.

    dry_run=True (default): no OF24 call, but full audit log is still written
    with status='dry_run'.
    """
    from app.services.repricing_stores import get_store, is_supported
    if not is_supported(store_key):
        return {"success": False, "msg": f"store not supported: {store_key}"}
    scfg = get_store(store_key)
    if scfg.get("offer_sync_only"):
        return {
            "success": False,
            "store_key": store_key,
            "msg": (f"store {store_key} is offer_sync_only - monitor requires "
                    f"Feishu pricing config; provision the Feishu Mirakl table "
                    f"and remove offer_sync_only flag first"),
        }
    formula_variant = scfg["formula_variant"]

    # The monitor's lightweight build_of24_payload only knows how to set
    # `price`. For push_discount stores (Lowes) a live push must ALSO move the
    # discounted price - sending a price-only payload would reset the offer's
    # discount. Refuse --live for those stores; the web dashboard's
    # OF21 + build_of24_payload_with_discount path is the supported route.
    if not dry_run and scfg.get("push_discount"):
        return {
            "success": False,
            "store_key": store_key,
            "msg": (f"--live not supported for push_discount store {store_key}; "
                    f"push from the /repricing/ web dashboard instead"),
            "aborted": True,
        }

    started = datetime.now()
    run_id = f"mon-{store_key}-{started.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

    # 1. supplier freshness gate
    freshness = get_supplier_freshness()
    if freshness["costway_stale"] or freshness["vevor_stale"]:
        return {
            "success": False,
            "store_key": store_key,
            "run_id": run_id,
            "msg": "supplier data stale",
            "freshness": {k: str(v) for k, v in freshness.items()},
            "aborted": True,
        }

    offers = fetch_active_offers(store_key)
    configs = fetch_pricing_configs(store_key)

    # 分档定价联动（用户2026-07-16定案）：有档位的SKU触发线=档位目标−2点，
    # 修价目标=该档公式价；没有档位记录的店铺/SKU沿用固定5%兜底线（macy不受影响）。
    tier_map: Dict[str, Dict[str, Any]] = {}
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """SELECT shop_sku, tier, target_margin
                     FROM order_system.pricing_tier WHERE store_key=%s""",
                (store_key,))
            tier_map = {r["shop_sku"]: r for r in cursor.fetchall() or []}
    except Exception:
        tier_map = {}
    finally:
        conn.close()

    print(f"[{run_id}] active offers: {len(offers)}, configs: {len(configs)}, "
          f"costway_max={freshness['costway_max']}, vevor_max={freshness['vevor_max']}")

    # 退货运费兜底值：SKU 缺 return_shipping_base 但已亏本时，用本店中位数先顶上，
    # 算个目标价放进待改价（打⚠️标记、只列不自动推），别让亏本被“配置缺失”盖住。
    _rb_vals = sorted(
        v for v in (_to_float(c.get("return_shipping_base")) for c in configs.values())
        if v is not None and v > 0
    )
    fallback_return_base = _rb_vals[len(_rb_vals) // 2] if _rb_vals else 20.0

    summary = {
        "total": len(offers),
        "skipped_blacklist": 0,
        "skipped_margin_ok": 0,
        "alert_no_config": 0,
        "alert_no_return_shipping": 0,
        "alert_no_cost": 0,
        "alert_cost_volatility": 0,
        "alert_loss_uncovered": 0,
        "dry_run_needs_config": 0,
        "triggered": 0,
        "dry_run_count": 0,
        "mirakl_success": 0,
        "mirakl_failed": 0,
    }

    api_call_seq = 0

    for ctx in offers:
        # 每个 SKU 重置：配置不全但已亏本 → 用兜底值算价进待改价、标记需人工、永不自动推
        needs_human = False
        config_notes = []
        # blacklist
        if is_blacklisted(ctx.shop_sku):
            _log(store_key, run_id, "auto_monitor", ctx, {
                "status": "skipped",
                "alert_type": "in_blacklist",
                "decision_reason": "in_blacklist",
            })
            summary["skipped_blacklist"] += 1
            continue

        # need warehouse_sku to look up Feishu config + supplier price
        if not ctx.warehouse_sku:
            _save_alert(ctx.shop_sku, store_key, "no_warehouse_sku",
                        "no mapping_table entry for this shop_sku")
            _log(store_key, run_id, "auto_monitor", ctx, {
                "status": "alert",
                "alert_type": "no_warehouse_sku",
                "decision_reason": "mapping_table missing entry",
            })
            summary["alert_no_config"] += 1
            continue

        cfg = configs.get(ctx.warehouse_sku)
        if not cfg:
            _save_alert(ctx.shop_sku, store_key, "feishu_config_missing",
                        f"no Feishu pricing config for warehouse_sku={ctx.warehouse_sku}")
            _log(store_key, run_id, "auto_monitor", ctx, {
                "status": "alert",
                "alert_type": "feishu_config_missing",
                "decision_reason": "no offer_pricing_config row",
            })
            summary["alert_no_config"] += 1
            continue

        supplier = cfg.get("supplier")
        return_base = _to_float(cfg.get("return_shipping_base"))
        # Store-level override (e.g. macy_kuyotq fixed at 0.4) wins over Feishu
        # per-SKU value to neutralise stale/dirty Feishu data.
        df_override = scfg.get("discount_factor_override")
        discount_factor = (
            float(df_override) if df_override is not None
            else _to_float(cfg.get("discount_factor"))
        )
        commission_rate = _to_float(cfg.get("commission_rate"))
        cost_buffer = _to_float(cfg.get("cost_buffer"))
        L = _to_float(cfg.get("length_in"))
        W = _to_float(cfg.get("width_in"))
        H = _to_float(cfg.get("height_in"))
        wt = _to_float(cfg.get("weight_lb"))
        # lowes_vevor(Yasonic)无退货运费项、不依赖尺寸——跳过 return_base/dim 门槛
        _is_vevor = formula_variant == "lowes_vevor"
        if _is_vevor:
            return_base = return_base if return_base is not None else 0.0
            if None in (L, W, H, wt):
                L, W, H, wt = (L or 0), (W or 0), (H or 0), (wt or 0)

        if return_base is None:
            # 精确改价需要退货运费基数。缺了：已亏本 → 用本店兜底值顶上、算价进待改价(标⚠️需人工、不自动推)；
            # 毛利健康只是缺配置 → 维持配置告警(不塞进待改价)。
            qc = _quick_loss_check(ctx, supplier, commission_rate, discount_factor, tier_map)
            if qc[0]:
                return_base = fallback_return_base
                needs_human = True
                config_notes.append(f"退货运费缺→用本店兜底${fallback_return_base:.0f}")
                # 不 continue，落到正常改价路径算目标价
            else:
                _save_alert(ctx.shop_sku, store_key, "return_shipping_missing",
                            "Feishu return_shipping_base is NULL")
                _log(store_key, run_id, "auto_monitor", ctx, {
                    "status": "alert",
                    "alert_type": "return_shipping_missing",
                    "decision_reason": "Feishu config has no return_shipping_base",
                    "supplier": supplier, "discount_factor": discount_factor,
                    "commission_rate": commission_rate,
                })
                summary["alert_no_return_shipping"] += 1
                continue

        if supplier not in ("Costway", "Vevor"):
            _save_alert(ctx.shop_sku, store_key, "unsupported_supplier",
                        f"supplier={supplier!r}")
            _log(store_key, run_id, "auto_monitor", ctx, {
                "status": "alert",
                "alert_type": "unsupported_supplier",
                "decision_reason": f"supplier {supplier!r} not supported",
                "supplier": supplier,
            })
            summary["alert_no_config"] += 1
            continue

        supplier_price, supplier_updated = lookup_supplier_price(ctx.warehouse_sku, supplier)
        if supplier_price is None:
            _save_alert(ctx.shop_sku, store_key, "cost_missing",
                        f"no row in supplier table for warehouse_sku={ctx.warehouse_sku}")
            _log(store_key, run_id, "auto_monitor", ctx, {
                "status": "alert",
                "alert_type": "cost_missing",
                "decision_reason": "supplier table lookup failed",
                "supplier": supplier,
            })
            summary["alert_no_cost"] += 1
            continue

        if not _is_vevor and (None in (L, W, H, wt) or any(v == 0 for v in (L, W, H))):
            # 缺尺寸算不了超大件退货附加费。已亏本(或前一道门槛已判需人工) → 尺寸按0(不计附加费)、
            # 算价进待改价标⚠️；毛利健康只是缺尺寸 → 维持配置告警。
            qc = (True,) if needs_human else _quick_loss_check(
                ctx, supplier, commission_rate, discount_factor, tier_map)
            if qc[0]:
                L, W, H, wt = 0.0, 0.0, 0.0, 0.0
                needs_human = True
                config_notes.append("尺寸缺→未计超大件退货附加费(需补尺寸)")
                # 不 continue
            else:
                _save_alert(ctx.shop_sku, store_key, "dim_missing",
                            f"L/W/H/wt missing or zero: {L},{W},{H},{wt}")
                _log(store_key, run_id, "auto_monitor", ctx, {
                    "status": "alert",
                    "alert_type": "dim_missing",
                    "decision_reason": "Feishu dim/weight missing",
                    "supplier": supplier, "supplier_price_db": supplier_price,
                })
                summary["alert_no_config"] += 1
                continue

        new_cost = cost_from_supplier_price(supplier_price, supplier)

        # cost volatility check vs last snapshot
        if cost_volatility_exceeds(ctx.last_cost_snapshot, new_cost,
                                     threshold=COST_VOLATILITY_THRESHOLD):
            pct = (
                abs(new_cost - ctx.last_cost_snapshot) / ctx.last_cost_snapshot
                if ctx.last_cost_snapshot else 0
            )
            # 成本大幅波动→不自动改价。已亏本(或前门槛已判需人工) → 算价进待改价标⚠️(需人工确认成本、不自动推)；
            # 毛利健康 → 维持波动告警。
            qc = (True,) if needs_human else _quick_loss_check(
                ctx, supplier, commission_rate, discount_factor, tier_map)
            if qc[0]:
                needs_human = True
                config_notes.append(f"成本较快照波动{pct:.1%}>30%(改价前人工确认成本)")
                # 不 continue
            else:
                _save_alert(ctx.shop_sku, store_key, "cost_volatility_30pct",
                            f"old={ctx.last_cost_snapshot:.2f} new={new_cost:.2f} pct={pct:.4f}")
                _log(store_key, run_id, "auto_monitor", ctx, {
                    "status": "alert",
                    "alert_type": "cost_volatility_30pct",
                    "decision_reason": f"cost moved {pct:.2%} > {COST_VOLATILITY_THRESHOLD:.0%}",
                    "supplier": supplier, "supplier_price_db": supplier_price,
                    "new_cost": new_cost, "cost_change_pct": pct,
                })
                summary["alert_cost_volatility"] += 1
                continue

        if ctx.db_origin_price is None or ctx.db_origin_price <= 0:
            _save_alert(ctx.shop_sku, store_key, "db_price_missing",
                        "origin_price NULL in DB - cannot compute margin")
            _log(store_key, run_id, "auto_monitor", ctx, {
                "status": "alert",
                "alert_type": "db_price_missing",
                "decision_reason": "DB origin_price is NULL/zero",
                "supplier": supplier, "supplier_price_db": supplier_price,
                "new_cost": new_cost,
            })
            summary["alert_no_cost"] += 1
            continue

        # compute current realised margin (at DB price + new cost)
        margin = realised_margin(
            current_origin_price=ctx.db_origin_price,
            supplier=supplier,
            supplier_price=supplier_price,
            return_shipping_base=return_base,
            discount_factor=discount_factor,
            commission_rate=commission_rate,
            length_in=L, width_in=W, height_in=H, weight_lb=wt,
            formula_variant=formula_variant, cost_buffer=cost_buffer,
        )

        common_log = {
            "supplier": supplier,
            "supplier_price_db": supplier_price,
            "supplier_data_age_hours": (
                round((datetime.now() - supplier_updated).total_seconds() / 3600, 2)
                if supplier_updated else None
            ),
            "costway_updated_at": (
                freshness["costway_max"] if supplier == "Costway" else None
            ),
            "vevor_updated_at": (
                freshness["vevor_max"] if supplier == "Vevor" else None
            ),
            "new_cost": round(new_cost, 4),
            "cost_change_pct": (
                round((new_cost - ctx.last_cost_snapshot) / ctx.last_cost_snapshot, 4)
                if ctx.last_cost_snapshot else None
            ),
            "discount_factor": discount_factor,
            "commission_rate": commission_rate,
            "return_shipping_base": return_base,
            "profit_margin_before": round(margin, 4),
        }

        # 分档联动：有档位 → 触发线=档位目标−2点、目标价=档位公式价；无档位 → 固定5%兜底
        trow = tier_map.get(ctx.shop_sku)
        tier_name = trow["tier"] if trow else None
        tier_target = _to_float(trow.get("target_margin")) if trow else None

        if tier_name == "delist":
            _update_cost_snapshot(ctx.shop_sku, new_cost)
            _log(store_key, run_id, "auto_monitor", ctx, {
                **common_log,
                "status": "skipped",
                "decision_reason": "定价方案判为下架档——监控不修价，等人工处理",
            })
            summary["skipped_margin_ok"] += 1
            continue

        if tier_target is not None and commission_rate is not None:
            threshold = tier_target - TIER_MARGIN_SLACK
            thr_desc = f"{tier_name}档目标{tier_target:.0%}−{TIER_MARGIN_SLACK:.0%}"
            divisor_override = 1.0 - commission_rate - tier_target
            margin_after = tier_target
        else:
            threshold = PROFIT_THRESHOLD
            thr_desc = f"固定兜底线{PROFIT_THRESHOLD:.0%}"
            divisor_override = None
            margin_after = 0.12

        if margin >= threshold:
            _update_cost_snapshot(ctx.shop_sku, new_cost)
            _log(store_key, run_id, "auto_monitor", ctx, {
                **common_log,
                "status": "skipped",
                "decision_reason": (
                    f"margin {margin:.4%} >= {threshold:.2%}（{thr_desc}）"
                ),
            })
            summary["skipped_margin_ok"] += 1
            continue

        # margin < threshold -> calculate target price（有档位=该档公式价）
        bd = calculate_breakdown(
            supplier=supplier,
            supplier_price=supplier_price,
            return_shipping_base=return_base,
            discount_factor=discount_factor,
            length_in=L, width_in=W, height_in=H, weight_lb=wt,
            formula_variant=formula_variant,
            cost_buffer=cost_buffer,
            divisor_override=divisor_override,
        )
        target_origin = round(bd.origin_price, 2)
        target_discount = round(bd.discount_price, 2)

        # OF24 payload + dry-run gate
        of24_offer = build_of24_payload(
            ctx, target_origin, target_discount,
            mode=get_store(store_key)["mode"],
        )
        payload_hash = _hash_payload(of24_offer)

        api_call_seq += 1
        if dry_run or needs_human:
            # needs_human：配置不全用兜底值算的价、或成本大幅波动——只列入待改价供人工核，永不自动推
            cfg_note = ("⚠️配置不全[" + "；".join(config_notes) + "] " if needs_human else "")
            tail = ("，仅列入待改价待人工核实(不自动推价)" if needs_human else "，dry_run=true")
            _log(store_key, run_id, "auto_monitor", ctx, {
                **common_log,
                "status": "dry_run",
                "alert_type": ("loss_needs_config" if needs_human else None),
                "decision_reason": (
                    cfg_note + f"margin {margin:.4%} < {threshold:.2%}（{thr_desc}）" + tail
                ),
                "new_origin_price": target_origin,
                "new_discount_price": target_discount,
                "return_shipping_extra": bd.return_shipping_extra,
                "return_cost_estimate": bd.return_cost_estimate,
                "total_cost": round(bd.total_cost, 4),
                "formula_calc_price": round(bd.formula_calc_price, 4),
                "target_origin_price": target_origin,
                "profit_margin_after": margin_after,    # 档位名义毛利（无档位=0.12）
                "mirakl_called": 0,
                "mirakl_payload_hash": payload_hash,
                "api_call_seq": api_call_seq,
            })
            summary["dry_run_count"] += 1
            if needs_human:
                summary["dry_run_needs_config"] += 1
            continue

        # real call
        try:
            resp = update_offers(store_key, [of24_offer], dry_run=False)
        except Exception as exc:
            new_count = _increment_failure(ctx.shop_sku, store_key, str(exc))
            _log(store_key, run_id, "auto_monitor", ctx, {
                **common_log,
                "status": "blacklisted" if new_count >= MAX_FAILURES_BEFORE_BLACKLIST else "failed",
                "alert_type": "api_failure",
                "decision_reason": str(exc),
                "new_origin_price": target_origin,
                "new_discount_price": target_discount,
                "return_shipping_extra": bd.return_shipping_extra,
                "return_cost_estimate": bd.return_cost_estimate,
                "total_cost": round(bd.total_cost, 4),
                "formula_calc_price": round(bd.formula_calc_price, 4),
                "target_origin_price": target_origin,
                "mirakl_called": 1,
                "mirakl_http_status": None,
                "mirakl_response_body": str(exc)[:1800],
                "mirakl_payload_hash": payload_hash,
                "api_call_seq": api_call_seq,
                "error_message": str(exc),
            })
            summary["mirakl_failed"] += 1
            continue

        http_status = resp.get("http_status")
        if resp.get("error") or http_status not in (200, 201):
            new_count = _increment_failure(
                ctx.shop_sku, store_key,
                f"http_status={http_status} body={resp.get('response_body','')[:200]}",
            )
            _log(store_key, run_id, "auto_monitor", ctx, {
                **common_log,
                "status": "blacklisted" if new_count >= MAX_FAILURES_BEFORE_BLACKLIST else "failed",
                "alert_type": "api_failure",
                "decision_reason": f"OF24 returned {http_status}",
                "new_origin_price": target_origin,
                "new_discount_price": target_discount,
                "return_shipping_extra": bd.return_shipping_extra,
                "return_cost_estimate": bd.return_cost_estimate,
                "total_cost": round(bd.total_cost, 4),
                "formula_calc_price": round(bd.formula_calc_price, 4),
                "target_origin_price": target_origin,
                "mirakl_called": 1,
                "mirakl_http_status": http_status,
                "mirakl_response_body": resp.get("response_body"),
                "mirakl_payload_hash": payload_hash,
                "api_call_seq": api_call_seq,
                "ip_used": resp.get("ip_used"),
            })
            summary["mirakl_failed"] += 1
            continue

        # OF24 HTTP 2xx = success (no separate verify step - the next-day OF52
        # cron pulling new origin_price is the existing sanity check).
        _update_origin_price(ctx.shop_sku, target_origin, target_discount, new_cost)
        _reset_failure(ctx.shop_sku)
        _log(store_key, run_id, "auto_monitor", ctx, {
            **common_log,
            "status": "success",
            "decision_reason": (
                f"margin {margin:.4%} < {threshold:.2%}（{thr_desc}）; pushed OF24 HTTP 2xx"
            ),
            "new_origin_price": target_origin,
            "new_discount_price": target_discount,
            "return_shipping_extra": bd.return_shipping_extra,
            "return_cost_estimate": bd.return_cost_estimate,
            "total_cost": round(bd.total_cost, 4),
            "formula_calc_price": round(bd.formula_calc_price, 4),
            "target_origin_price": target_origin,
            "profit_margin_after": margin_after,
            "mirakl_called": 1,
            "mirakl_import_id": resp.get("import_id"),
            "mirakl_http_status": http_status,
            "mirakl_response_body": resp.get("response_body"),
            "mirakl_payload_hash": payload_hash,
            "api_call_seq": api_call_seq,
            "ip_used": resp.get("ip_used"),
        })
        summary["mirakl_success"] += 1
        summary["triggered"] += 1

    summary["duration_seconds"] = round((datetime.now() - started).total_seconds(), 2)
    summary["run_id"] = run_id
    summary["store_key"] = store_key
    summary["dry_run"] = dry_run
    return summary
