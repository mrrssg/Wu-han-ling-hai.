"""
Production single-SKU push: OF22 -> build OF24 payload preserving every
existing field -> OF24 with the new price.

Designed as the P8 verification path. Use with extreme care:

    PYTHONPATH=/var/www/autoweb/AutoWeb FLASK_CONFIG=production \
        ./venv/bin/python scripts/push_single_offer.py \
        --store macy_kuyotq --shop_sku MRMC575443 --confirm-live

Without `--confirm-live`, runs in dry-run mode (no OF24 call, no DB write,
prints the would-be payload).

Total runtime per SKU = 2 * cooldown_seconds (~130s) because OF22 + OF24
share the 65s rate lock on the macy_kuyotq channel.
"""
import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app import create_app
from app.models.db_manager import DBManager
from app.services.mirakl_offer_api_service import get_offer, update_offers
from app.services.repricing_formula import (
    calculate_breakdown,
    cost_from_supplier_price,
    realised_margin,
)
from app.services.repricing_monitor_service import (
    fetch_active_offers,
    fetch_pricing_configs,
    get_supplier_freshness,
    lookup_supplier_price,
    _log,
)


SUPPORTED = {"macy_kuyotq"}


# =============================================================================
# OF24 payload from OF22 - field-by-field, preserving everything except price
# =============================================================================

def _flat_logistic(v):
    if isinstance(v, dict):
        return v.get("code")
    return v


def _flat_product_refs(refs):
    if not isinstance(refs, list) or not refs:
        return None, None
    first = refs[0]
    if not isinstance(first, dict):
        return None, None
    return first.get("reference"), first.get("reference_type")


def build_of24_payload_from_of22(of22: dict, new_price: float) -> dict:
    """Reconstruct an OF24 update payload that touches NOTHING except price.

    Mirakl docs are explicit: fields not provided are reset to default. So we
    have to copy every preserved field across.
    """
    product_id, product_id_type = _flat_product_refs(of22.get("product_references"))

    payload = {
        "shop_sku": of22.get("shop_sku"),
        "state_code": of22.get("state_code"),
        "update_delete": "update",

        # the field we're actually changing
        "price": round(float(new_price), 2),

        # quantity gets RESET TO 0 if missing -> preserve
        "quantity": of22.get("quantity"),

        # optional at update; preserve for safety
        "product_id": product_id,
        "product_id_type": product_id_type,

        # all the fields that get DELETED if missing per OF24 docs
        "available_started": of22.get("available_start_date"),
        "available_ended": of22.get("available_end_date"),
        "description": of22.get("description"),
        "discount": of22.get("discount"),
        "eco_contributions": of22.get("eco_contributions"),
        "internal_description": of22.get("internal_description"),
        "leadtime_to_ship": of22.get("leadtime_to_ship"),
        "logistic_class": _flat_logistic(of22.get("logistic_class")),
        "max_order_quantity": of22.get("max_order_quantity"),
        "min_order_quantity": of22.get("min_order_quantity"),
        "min_quantity_alert": of22.get("min_quantity_alert"),
        "offer_additional_fields": of22.get("offer_additional_fields"),
        "package_quantity": of22.get("package_quantity"),
        "price_additional_info": of22.get("price_additional_info"),
        "product_tax_code": of22.get("product_tax_code"),
    }

    # Strip keys whose value is None to avoid Mirakl rejecting null on
    # fields it considers "not provided" anyway. allow_quote_requests is
    # special: missing means false; preserve only if non-default.
    cleaned = {k: v for k, v in payload.items() if v is not None}

    # allow_quote_requests has different default-rewrite semantics; preserve it
    aqr = of22.get("allow_quote_requests")
    if aqr is not None:
        cleaned["allow_quote_requests"] = bool(aqr)

    return cleaned


# =============================================================================
# Single SKU pipeline
# =============================================================================

def push_one(store_key: str, shop_sku: str, confirm_live: bool) -> dict:
    if store_key not in SUPPORTED:
        return {"success": False, "msg": f"store not enabled: {store_key}"}

    # 1. Resolve all the context we need
    offers = fetch_active_offers(store_key)
    ctx = next((o for o in offers if o.shop_sku == shop_sku), None)
    if not ctx:
        return {"success": False, "msg": f"shop_sku not active in DB: {shop_sku}"}
    if not ctx.warehouse_sku:
        return {"success": False, "msg": "no warehouse_sku mapping for this shop_sku"}
    if not ctx.raw_json:
        return {"success": False, "msg": "no raw_json snapshot in DB"}

    raw = json.loads(ctx.raw_json)
    offer_id = raw.get("offer_id")
    if not offer_id:
        return {"success": False, "msg": "no offer_id in raw_json"}

    configs = fetch_pricing_configs(store_key)
    cfg = configs.get(ctx.warehouse_sku)
    if not cfg:
        return {"success": False, "msg": "no Feishu pricing config"}

    supplier = cfg["supplier"]
    if supplier not in ("Costway", "Vevor"):
        return {"success": False, "msg": f"unsupported supplier: {supplier}"}
    rb = float(cfg["return_shipping_base"]) if cfg.get("return_shipping_base") is not None else None
    if rb is None:
        return {"success": False, "msg": "no return_shipping_base in Feishu config"}

    sp, sp_at = lookup_supplier_price(ctx.warehouse_sku, supplier)
    if sp is None:
        return {"success": False, "msg": f"no supplier price for {ctx.warehouse_sku}"}

    freshness = get_supplier_freshness()
    if freshness["costway_stale"] or freshness["vevor_stale"]:
        return {"success": False, "msg": "supplier data stale - refusing to push"}

    L = float(cfg["length_in"])
    W = float(cfg["width_in"])
    H = float(cfg["height_in"])
    wt = float(cfg["weight_lb"])

    new_cost = cost_from_supplier_price(sp, supplier)
    margin = realised_margin(
        current_origin_price=ctx.db_origin_price,
        supplier=supplier,
        supplier_price=sp,
        return_shipping_base=rb,
        discount_factor=float(cfg["discount_factor"]),
        commission_rate=float(cfg["commission_rate"]),
        length_in=L, width_in=W, height_in=H, weight_lb=wt,
    )

    bd = calculate_breakdown(
        supplier=supplier,
        supplier_price=sp,
        return_shipping_base=rb,
        discount_factor=float(cfg["discount_factor"]),
        length_in=L, width_in=W, height_in=H, weight_lb=wt,
    )
    target_origin = round(float(bd.origin_price), 2)

    print(f"\n=== Pricing summary for {shop_sku} ===")
    print(f"  warehouse_sku:        {ctx.warehouse_sku}")
    print(f"  supplier:             {supplier}")
    print(f"  supplier_price (DB):  {sp}  (updated_at={sp_at})")
    print(f"  new_cost:             {new_cost:.4f}")
    print(f"  current_origin_price: {ctx.db_origin_price}")
    print(f"  target_origin_price:  {target_origin}")
    print(f"  current_margin:       {margin:.4f}")
    print(f"  target_margin:        ~0.12 (formula default)")

    # 2. OF22 to fetch full offer details
    print(f"\n=== Calling OF22 (offer_id={offer_id}) ===")
    t0 = time.time()
    of22 = get_offer(store_key, int(offer_id))
    print(f"  OF22 ok, {time.time() - t0:.1f}s")
    print(f"  shop_sku={of22.get('shop_sku')}  state_code={of22.get('state_code')}  "
          f"quantity={of22.get('quantity')}  current price={of22.get('price')}")
    print(f"  description present={bool(of22.get('description'))}  "
          f"leadtime={of22.get('leadtime_to_ship')}  "
          f"logistic_class={of22.get('logistic_class')}")

    # Sanity: OF22 shop_sku should match
    if of22.get("shop_sku") != shop_sku:
        return {"success": False, "msg": "OF22 returned different shop_sku - aborting"}

    # 3. Build OF24 payload
    payload_offer = build_of24_payload_from_of22(of22, target_origin)

    print(f"\n=== OF24 payload preview ===")
    print(json.dumps(payload_offer, ensure_ascii=False, indent=2)[:2000])

    if not confirm_live:
        print(f"\n[DRY RUN] not calling OF24. Pass --confirm-live to actually push.")
        return {"success": True, "dry_run": True, "shop_sku": shop_sku,
                "payload": payload_offer}

    # 4. OF24 real call
    print(f"\n=== Calling OF24 (LIVE) ===")
    t1 = time.time()
    resp = update_offers(store_key, [payload_offer], dry_run=False)
    print(f"  OF24 returned in {time.time() - t1:.1f}s")
    print(json.dumps(resp, ensure_ascii=False, indent=2, default=str))

    # 5. Log result
    run_id = f"single-{shop_sku}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    http_status = resp.get("http_status")
    success = http_status in (200, 201) and not resp.get("error")
    status_str = "pending_verify" if success else "failed"

    _log(store_key, run_id, "manual_single", ctx, {
        "supplier": supplier,
        "supplier_price_db": sp,
        "new_cost": round(new_cost, 4),
        "new_origin_price": target_origin,
        "new_discount_price": round(target_origin * float(cfg["discount_factor"]), 2),
        "discount_factor": float(cfg["discount_factor"]),
        "commission_rate": float(cfg["commission_rate"]),
        "return_shipping_base": rb,
        "return_shipping_extra": bd.return_shipping_extra,
        "return_cost_estimate": bd.return_cost_estimate,
        "total_cost": round(bd.total_cost, 4),
        "formula_calc_price": round(bd.formula_calc_price, 4),
        "target_origin_price": target_origin,
        "profit_margin_before": round(margin, 4),
        "profit_margin_after": 0.12,
        "mirakl_called": 1,
        "mirakl_import_id": resp.get("import_id"),
        "mirakl_http_status": http_status,
        "mirakl_response_body": resp.get("response_body"),
        "ip_used": resp.get("ip_used"),
        "status": status_str,
        "decision_reason": f"single-SKU test push from CLI; old margin={margin:.4%}",
        "error_message": resp.get("error"),
    })

    # 6. Update offerprice_listing if successful
    if success:
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """UPDATE order_system.offerprice_listing
                          SET origin_price=%s,
                              last_cost_snapshot=%s,
                              last_cost_snapshot_at=%s
                        WHERE shop_sku=%s
                          AND platform='Macy' AND shop_name='kuyotq'""",
                    (
                        target_origin,
                        new_cost,
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        shop_sku,
                    ),
                )
            conn.commit()
            print(f"\n  DB updated: origin_price={target_origin}, last_cost_snapshot={new_cost:.4f}")
        finally:
            conn.close()

    return {
        "success": success,
        "shop_sku": shop_sku,
        "old_origin_price": float(ctx.db_origin_price),
        "new_origin_price": target_origin,
        "old_margin": round(margin, 4),
        "import_id": resp.get("import_id"),
        "http_status": http_status,
        "status": status_str,
        "run_id": run_id,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Single-SKU OF24 push for verification.")
    parser.add_argument("--store", default="macy_kuyotq")
    parser.add_argument("--shop_sku", required=True)
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="REAL OF24 push. Without this flag the script runs in dry-run.",
    )
    args = parser.parse_args()

    config_name = os.environ.get("FLASK_CONFIG", "production")
    app = create_app(config_name)
    with app.app_context():
        try:
            result = push_one(args.store, args.shop_sku, args.confirm_live)
        except Exception as exc:
            result = {"success": False, "error": str(exc)}

    print("\n=== Result ===")
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
