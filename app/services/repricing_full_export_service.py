"""
Part 2: weekly full repricing export.

For every active macy_kuyotq offer:
  1. Calculate target origin_price using the latest supplier cost + Feishu
     config (returns 12% margin per the formula in repricing_formula).
  2. Compare against current DB origin_price.
  3. If different (delta >= $0.01), include the row in the output xlsx.
  4. If same, skip - no point uploading an unchanged price.

The output is a Mirakl-compatible `offers-import` xlsx that the operator
downloads and uploads to the Mirakl portal manually. We do NOT call OF24
from this path - this is the deliberate user choice.

Performance: this service pre-loads all required data in batch so the inner
loop is pure-Python with zero per-SKU DB roundtrips. 3000+ offers should
finish in well under a minute.

Audit trail: every iteration (skipped / would-update / blocked-by-alert)
writes a row to offer_price_change_log with run_type='full_export', so the
operator can review what was decided without opening the xlsx.
"""
import json
import os
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from openpyxl import Workbook

from app.models.db_manager import DBManager
from app.services.repricing_formula import (
    calculate_breakdown,
    cost_from_supplier_price,
)
from app.services.repricing_monitor_service import (
    MAX_SUPPLIER_STALE_HOURS,
    get_supplier_freshness,
)


STORE_KEY = "macy_kuyotq"

# Column order MUST match the Mirakl offers-import template; if anything in
# this list deviates from the sample uploaded by the operator, Mirakl will
# reject the file at validation.
OFFERS_IMPORT_COLUMNS = [
    "sku", "product-id", "product-id-type", "description",
    "internal-description", "price", "price-additional-info", "quantity",
    "min-quantity-alert", "state", "available-start-date",
    "available-end-date", "logistic-class", "favorite-rank",
    "discount-start-date", "discount-end-date", "discount-price",
    "update-delete", "leadtime-to-ship",
]

MIN_PRICE_DELTA = 0.01    # ignore drift below 1¢


# =============================================================================
# Bulk preload helpers - replace per-SKU queries
# =============================================================================

def _load_active_offers() -> List[Dict]:
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """SELECT shop_sku, warehouse_sku, origin_price, raw_json,
                          state_code, quantity, last_cost_snapshot
                     FROM order_system.offerprice_listing
                    WHERE platform='Macy' AND shop_name='kuyotq' AND active=1""",
            )
            return cursor.fetchall() or []
    finally:
        conn.close()


def _load_pricing_configs() -> Dict[str, Dict]:
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """SELECT warehouse_sku, supplier, discount_factor, commission_rate,
                          return_shipping_base, length_in, width_in, height_in, weight_lb
                     FROM order_system.offer_pricing_config
                    WHERE store_key=%s""",
                (STORE_KEY,),
            )
            return {r["warehouse_sku"]: r for r in cursor.fetchall() or []}
    finally:
        conn.close()


def _load_supplier_prices() -> Dict[Tuple[str, str], Tuple[float, Any]]:
    """Return {(supplier, sku): (Price, Updated_At)} for both supplier tables."""
    out: Dict[Tuple[str, str], Tuple[float, Any]] = {}
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT SKU, Price, Updated_At FROM autooperate.newestdropship"
            )
            for r in cursor.fetchall() or []:
                sku = (r.get("SKU") or "").strip()
                if not sku:
                    continue
                p = r.get("Price")
                try:
                    p = float(p) if p is not None else None
                except (TypeError, ValueError):
                    p = None
                if p is not None:
                    out[("Costway", sku)] = (p, r.get("Updated_At"))

            cursor.execute(
                "SELECT SKU, Price, Updated_At FROM autooperate.newestdropship_vevor"
            )
            for r in cursor.fetchall() or []:
                sku = (r.get("SKU") or "").strip()
                if not sku:
                    continue
                p = r.get("Price")
                try:
                    p = float(p) if p is not None else None
                except (TypeError, ValueError):
                    p = None
                if p is not None:
                    out[("Vevor", sku)] = (p, r.get("Updated_At"))
    finally:
        conn.close()
    return out


def _load_blacklist() -> set:
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """SELECT shop_sku FROM order_system.offer_alert_state
                    WHERE store_key=%s AND blacklisted=1""",
                (STORE_KEY,),
            )
            return {r["shop_sku"] for r in cursor.fetchall() or []}
    finally:
        conn.close()


# =============================================================================
# Per-row decision (pure-python, no DB)
# =============================================================================

def _decide(offer: Dict, cfg: Optional[Dict], sp_lookup: Dict, blacklist: set) -> Dict[str, Any]:
    """Decide what to do with one offer; returns a result dict with one of
    these statuses:
      - 'skipped_blacklist'
      - 'alert_no_config'
      - 'alert_no_return_shipping'
      - 'alert_no_dim'
      - 'alert_no_supplier_price'
      - 'alert_unsupported_supplier'
      - 'skipped_same_price'        (no change needed)
      - 'would_update'              (include in xlsx)
    """
    shop_sku = offer["shop_sku"]
    warehouse_sku = offer.get("warehouse_sku")
    db_origin_price = offer.get("origin_price")

    if shop_sku in blacklist:
        return {"status": "skipped_blacklist"}

    if not warehouse_sku:
        return {"status": "alert_no_config", "alert_type": "no_warehouse_sku"}

    if not cfg:
        return {"status": "alert_no_config", "alert_type": "feishu_config_missing"}

    supplier = cfg.get("supplier")
    if supplier not in ("Costway", "Vevor"):
        return {"status": "alert_unsupported_supplier", "alert_type": "unsupported_supplier"}

    rb = cfg.get("return_shipping_base")
    if rb is None:
        return {"status": "alert_no_return_shipping", "alert_type": "return_shipping_missing"}
    rb = float(rb)

    L = cfg.get("length_in"); W = cfg.get("width_in")
    H = cfg.get("height_in"); wt = cfg.get("weight_lb")
    if None in (L, W, H, wt) or any(float(v) == 0 for v in (L, W, H)):
        return {"status": "alert_no_dim", "alert_type": "dim_missing"}

    sp_info = sp_lookup.get((supplier, warehouse_sku))
    if not sp_info:
        return {"status": "alert_no_supplier_price", "alert_type": "cost_missing"}
    supplier_price, supplier_updated = sp_info

    new_cost = cost_from_supplier_price(supplier_price, supplier)
    df = float(cfg["discount_factor"])
    cr = float(cfg["commission_rate"])

    bd = calculate_breakdown(
        supplier=supplier,
        supplier_price=supplier_price,
        return_shipping_base=rb,
        discount_factor=df,
        length_in=float(L), width_in=float(W),
        height_in=float(H), weight_lb=float(wt),
    )
    target_origin = round(float(bd.origin_price), 2)
    target_discount = round(float(bd.discount_price), 2)

    if db_origin_price is None:
        # treat as needs-update so the operator can fill it in
        delta = None
    else:
        delta = abs(target_origin - float(db_origin_price))

    if delta is not None and delta < MIN_PRICE_DELTA:
        return {
            "status": "skipped_same_price",
            "supplier": supplier,
            "supplier_price": supplier_price,
            "supplier_updated": supplier_updated,
            "new_cost": new_cost,
            "target_origin_price": target_origin,
            "current_origin_price": float(db_origin_price),
            "delta": delta,
        }

    return {
        "status": "would_update",
        "supplier": supplier,
        "supplier_price": supplier_price,
        "supplier_updated": supplier_updated,
        "new_cost": new_cost,
        "target_origin_price": target_origin,
        "target_discount_price": target_discount,
        "current_origin_price": float(db_origin_price) if db_origin_price is not None else None,
        "delta": delta,
        "return_shipping_extra": bd.return_shipping_extra,
        "return_cost_estimate": bd.return_cost_estimate,
        "total_cost": bd.total_cost,
        "formula_calc_price": bd.formula_calc_price,
        "discount_factor": df,
        "commission_rate": cr,
        "return_shipping_base": rb,
    }


# =============================================================================
# Excel writer
# =============================================================================

def _build_xlsx_row(offer: Dict, decision: Dict, raw: Dict) -> Dict[str, Any]:
    """Build one row of the offers-import xlsx. Mirakl ignores empty columns
    for fields we don't want to touch.
    """
    return {
        "sku": offer["shop_sku"],
        "product-id": raw.get("product_sku") or offer["shop_sku"],
        "product-id-type": "SKU",
        "description": None,
        "internal-description": None,
        "price": decision["target_origin_price"],
        "price-additional-info": None,
        "quantity": raw.get("quantity") or offer.get("quantity") or 0,
        "min-quantity-alert": None,
        "state": raw.get("state_code") or offer.get("state_code") or "11",
        "available-start-date": None,
        "available-end-date": None,
        "logistic-class": (
            (raw.get("logistic_class") or {}).get("code")
            if isinstance(raw.get("logistic_class"), dict)
            else None
        ),
        "favorite-rank": None,
        "discount-start-date": None,
        "discount-end-date": None,
        "discount-price": None,
        "update-delete": "update",
        "leadtime-to-ship": raw.get("leadtime_to_ship"),
    }


def write_xlsx(rows: List[Dict[str, Any]], output_path: str) -> int:
    wb = Workbook()
    ws = wb.active
    ws.title = "offers-import"
    ws.append(OFFERS_IMPORT_COLUMNS)
    for r in rows:
        ws.append([r.get(c) for c in OFFERS_IMPORT_COLUMNS])
    wb.save(output_path)
    return len(rows)


# =============================================================================
# Audit log
# =============================================================================

def _log_decisions(run_id: str, decisions: List[Tuple[Dict, Dict]]):
    """Bulk-insert decision rows into offer_price_change_log."""
    if not decisions:
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    rows = []
    for offer, d in decisions:
        status = d["status"]
        status_for_log = {
            "skipped_blacklist": "skipped",
            "alert_no_config": "alert",
            "alert_no_return_shipping": "alert",
            "alert_no_dim": "alert",
            "alert_no_supplier_price": "alert",
            "alert_unsupported_supplier": "alert",
            "skipped_same_price": "skipped",
            "would_update": "dry_run",   # not pushed yet; "dry_run" matches existing taxonomy
        }.get(status, "skipped")

        rows.append((
            run_id,
            "full_export",
            STORE_KEY,
            offer["shop_sku"],
            offer.get("warehouse_sku"),
            now,
            status_for_log,
            (
                "calc==db, no update needed" if status == "skipped_same_price"
                else "blacklisted" if status == "skipped_blacklist"
                else f"calc={d.get('target_origin_price')} vs db={d.get('current_origin_price')}, delta={d.get('delta')}" if status == "would_update"
                else status
            ),
            d.get("alert_type"),
            d.get("supplier"),
            d.get("supplier_price"),
            d.get("new_cost"),
            d.get("current_origin_price"),
            d.get("target_origin_price"),
            d.get("target_discount_price"),
            d.get("discount_factor"),
            d.get("commission_rate"),
            d.get("return_shipping_base"),
            d.get("return_shipping_extra"),
            d.get("return_cost_estimate"),
            d.get("total_cost"),
            d.get("formula_calc_price"),
            d.get("target_origin_price"),
        ))

    sql = """
        INSERT INTO order_system.offer_price_change_log
            (run_id, run_type, store_key, shop_sku, warehouse_sku, triggered_at,
             status, decision_reason, alert_type,
             supplier, supplier_price_db, new_cost,
             old_origin_price, new_origin_price, new_discount_price,
             discount_factor, commission_rate, return_shipping_base,
             return_shipping_extra, return_cost_estimate, total_cost,
             formula_calc_price, target_origin_price)
        VALUES (%s, %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s)
    """
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            chunk = 500
            for i in range(0, len(rows), chunk):
                cursor.executemany(sql, rows[i:i + chunk])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# =============================================================================
# Top-level entry
# =============================================================================

def run_full_export(output_dir: str) -> Dict[str, Any]:
    """Top-level entry. Returns a summary dict including output_file path.
    Aborts if supplier data is stale.
    """
    started = datetime.now()
    run_id = f"full-{STORE_KEY}-{started.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

    freshness = get_supplier_freshness()
    if freshness["costway_stale"] or freshness["vevor_stale"]:
        return {
            "success": False,
            "run_id": run_id,
            "msg": "supplier data stale; refusing to export",
            "freshness": {
                "costway_max": str(freshness["costway_max"]),
                "vevor_max": str(freshness["vevor_max"]),
                "threshold_hours": freshness["threshold_hours"],
            },
        }

    print(f"[{run_id}] preloading all data ...")
    active_offers = _load_active_offers()
    configs = _load_pricing_configs()
    sp_lookup = _load_supplier_prices()
    blacklist = _load_blacklist()
    print(f"  active offers : {len(active_offers)}")
    print(f"  configs       : {len(configs)}")
    print(f"  supplier prices: {len(sp_lookup)}")
    print(f"  blacklist     : {len(blacklist)}")

    decisions: List[Tuple[Dict, Dict]] = []
    xlsx_rows: List[Dict[str, Any]] = []

    summary = {
        "total_offers": len(active_offers),
        "would_update": 0,
        "skipped_same_price": 0,
        "skipped_blacklist": 0,
        "alert_no_config": 0,
        "alert_no_return_shipping": 0,
        "alert_no_dim": 0,
        "alert_no_supplier_price": 0,
        "alert_unsupported_supplier": 0,
    }

    for offer in active_offers:
        wh = offer.get("warehouse_sku")
        cfg = configs.get(wh) if wh else None
        decision = _decide(offer, cfg, sp_lookup, blacklist)
        decisions.append((offer, decision))
        summary[decision["status"]] = summary.get(decision["status"], 0) + 1

        if decision["status"] == "would_update":
            raw = {}
            if offer.get("raw_json"):
                try:
                    raw = json.loads(offer["raw_json"])
                except (TypeError, ValueError):
                    raw = {}
            xlsx_rows.append(_build_xlsx_row(offer, decision, raw))

    # Sort xlsx output by sku for tidy review
    xlsx_rows.sort(key=lambda r: r["sku"])

    os.makedirs(output_dir, exist_ok=True)
    fname = f"macy_kuyotq_repricing_{started.strftime('%Y%m%d_%H%M%S')}.xlsx"
    output_path = os.path.join(output_dir, fname)
    written = write_xlsx(xlsx_rows, output_path) if xlsx_rows else 0
    if not xlsx_rows:
        # write empty file with just header so download still works
        write_xlsx([], output_path)

    _log_decisions(run_id, decisions)

    duration = (datetime.now() - started).total_seconds()
    return {
        "success": True,
        "run_id": run_id,
        "output_file": output_path,
        "filename": fname,
        "rows_written": written,
        "summary": summary,
        "duration_seconds": round(duration, 2),
        "freshness": {
            "costway_max": str(freshness["costway_max"]),
            "vevor_max": str(freshness["vevor_max"]),
        },
    }
