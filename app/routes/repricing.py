"""
Web routes for the automated repricing system. Multi-store: every page and
API accepts a `store` query param (macy_kuyotq | lowes_autool), defaulting to
macy_kuyotq. Store config lives in repricing_stores.REPRICING_STORES.

Pages:
    /repricing/?store=X          dashboard (overview + KPIs + recent activity)
    /repricing/candidates?store=X SKUs that would trigger Part 1 repricing
    /repricing/alerts?store=X    all alert rows (feishu_config_missing etc.)
    /repricing/changes?store=X   full change-log history with filters
    /repricing/blacklist?store=X blacklist + per-SKU alert state
    /repricing/full-export?store=X  Part 2 Excel generation

APIs:
    POST /repricing/push/<shop_sku>?store=X     manual single-SKU push
    POST /repricing/push-batch?store=X          batched push
    POST /repricing/blacklist/<shop_sku>/clear  unblock + reset failure count

All read endpoints are GET; only writes use POST. No Mirakl API call is made
from a GET path - GETs only read autoweb DB.
"""
import json
import os
import sys
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, render_template, request, redirect, url_for, flash, current_app

from app.models.db_manager import DBManager
from app.services.repricing_monitor_service import (
    get_supplier_freshness,
    fetch_active_offers,
    fetch_pricing_configs,
    lookup_supplier_price,
)
from app.services.repricing_formula import (
    calculate_breakdown,
    cost_from_supplier_price,
    realised_margin,
)
from app.services.repricing_stores import (
    REPRICING_STORES,
    get_store,
    is_supported,
    store_options,
)


repricing_bp = Blueprint("repricing", __name__)

DEFAULT_STORE = "macy_kuyotq"


def _current_store() -> str:
    """Resolve the active store from ?store= (GET) or form/json (POST).
    Falls back to macy_kuyotq. Always validated against REPRICING_STORES.
    """
    s = (request.args.get("store") or request.form.get("store") or "").strip()
    if not s:
        payload = request.get_json(silent=True) or {}
        s = (payload.get("store") or "").strip()
    if not s or not is_supported(s):
        return DEFAULT_STORE
    return s


# =============================================================================
# Helpers - read-only DB queries
# =============================================================================

def _query(sql: str, params=None) -> List[Dict]:
    """Run a SELECT and return rows. Pass params as a tuple; never pass ()
    because pymysql then interprets `%` in the SQL (e.g. inside LIKE patterns)
    as format placeholders and explodes with "not enough arguments".
    """
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            if params:
                cursor.execute(sql, params)
            else:
                cursor.execute(sql)
            return cursor.fetchall() or []
    finally:
        conn.close()


def _summary_counts(store_key: str) -> Dict[str, int]:
    """Top-level counts for the dashboard."""
    scfg = get_store(store_key)
    run_like = f"mon-{store_key}-%"

    rows = _query(
        """SELECT SUM(active=1) AS active_, COUNT(*) AS total
             FROM order_system.offerprice_listing
            WHERE platform=%s AND shop_name=%s""",
        (scfg["platform"], scfg["shop_name"]),
    )
    base = rows[0] if rows else {}

    latest = _query(
        """SELECT MAX(run_id) AS run_id, MAX(triggered_at) AS run_at
             FROM order_system.offer_price_change_log
            WHERE run_id LIKE %s""",
        (run_like,),
    )
    latest_run = latest[0] if latest else {}
    latest_run_id = latest_run.get("run_id")

    by_status = _query(
        """SELECT status, COUNT(*) c
             FROM order_system.offer_price_change_log
            WHERE run_id = %s GROUP BY status""",
        (latest_run_id,),
    ) if latest_run_id else []
    status_map = {r["status"]: int(r["c"]) for r in by_status}

    pending_dry_run = 0
    if latest_run_id:
        pending_row = _query(
            """SELECT COUNT(*) c FROM order_system.offer_price_change_log log
                WHERE log.run_id=%s AND log.status='dry_run'
                  AND NOT EXISTS (
                      SELECT 1 FROM order_system.offer_price_change_log later
                       WHERE later.shop_sku = log.shop_sku
                         AND later.store_key = %s
                         AND later.triggered_at > log.triggered_at
                         AND later.status = 'success'
                  )""",
            (latest_run_id, store_key),
        )
        pending_dry_run = int(pending_row[0]["c"]) if pending_row else 0

    blk = _query(
        """SELECT COUNT(*) c FROM order_system.offer_alert_state
            WHERE store_key=%s AND blacklisted=1""",
        (store_key,),
    )
    blacklist_n = int(blk[0]["c"]) if blk else 0

    success_today = _query(
        """SELECT COUNT(*) c FROM order_system.offer_price_change_log
            WHERE store_key=%s AND status='success'
              AND DATE(triggered_at)=CURDATE()""",
        (store_key,),
    )
    success_today_n = int(success_today[0]["c"]) if success_today else 0

    return {
        "total_offers": int(base.get("total") or 0),
        "active_offers": int(base.get("active_") or 0),
        "latest_run_id": latest_run_id,
        "latest_run_at": latest_run.get("run_at"),
        "latest_skipped": status_map.get("skipped", 0),
        "latest_alert": status_map.get("alert", 0),
        "latest_dry_run_raw": status_map.get("dry_run", 0),
        "latest_dry_run": pending_dry_run,
        "pushed_since_run": status_map.get("dry_run", 0) - pending_dry_run,
        "blacklist_count": blacklist_n,
        "success_today": success_today_n,
    }


def _top_candidates(store_key: str, limit: int = 10) -> List[Dict]:
    """dry_run SKUs from the latest monitor run, minus already-pushed."""
    run_like = f"mon-{store_key}-%"
    latest = _query(
        """SELECT run_id FROM order_system.offer_price_change_log
            WHERE run_id LIKE %s AND status='dry_run'
            ORDER BY triggered_at DESC LIMIT 1""",
        (run_like,),
    )
    if not latest:
        return []
    return _query(
        """SELECT log.shop_sku, log.warehouse_sku, log.supplier,
                  log.old_origin_price, log.new_origin_price, log.new_cost,
                  log.profit_margin_before, log.return_shipping_base,
                  log.supplier_price_db
             FROM order_system.offer_price_change_log log
            WHERE log.run_id=%s AND log.status='dry_run'
              AND NOT EXISTS (
                  SELECT 1 FROM order_system.offer_price_change_log later
                   WHERE later.shop_sku = log.shop_sku
                     AND later.store_key = %s
                     AND later.triggered_at > log.triggered_at
                     AND later.status = 'success'
              )
            ORDER BY log.profit_margin_before ASC
            LIMIT %s""",
        (latest[0]["run_id"], store_key, limit),
    )


def _all_candidates(store_key: str) -> List[Dict]:
    """Every dry_run row from the latest run not yet pushed."""
    run_like = f"mon-{store_key}-%"
    latest = _query(
        """SELECT run_id FROM order_system.offer_price_change_log
            WHERE run_id LIKE %s AND status='dry_run'
            ORDER BY triggered_at DESC LIMIT 1""",
        (run_like,),
    )
    if not latest:
        return []
    return _query(
        """SELECT log.* FROM order_system.offer_price_change_log log
            WHERE log.run_id=%s AND log.status='dry_run'
              AND NOT EXISTS (
                  SELECT 1 FROM order_system.offer_price_change_log later
                   WHERE later.shop_sku = log.shop_sku
                     AND later.store_key = %s
                     AND later.triggered_at > log.triggered_at
                     AND later.status = 'success'
              )
            ORDER BY log.profit_margin_before ASC""",
        (latest[0]["run_id"], store_key),
    )


def _alerts(store_key: str) -> List[Dict]:
    """Currently unresolved alert state per SKU."""
    return _query(
        """SELECT shop_sku, last_alert_type, last_alert_message, last_alert_at,
                  failure_count, blacklisted, blacklisted_at, blacklisted_reason
             FROM order_system.offer_alert_state
            WHERE store_key=%s AND (resolved_at IS NULL OR last_alert_at > resolved_at)
            ORDER BY last_alert_at DESC""",
        (store_key,),
    )


def _alert_breakdown_latest_run(store_key: str) -> List[Dict]:
    """alert_type counts from the latest monitor run."""
    run_like = f"mon-{store_key}-%"
    latest = _query(
        """SELECT run_id FROM order_system.offer_price_change_log
            WHERE run_id LIKE %s AND status='alert'
            ORDER BY triggered_at DESC LIMIT 1""",
        (run_like,),
    )
    if not latest:
        return []
    return _query(
        """SELECT alert_type, COUNT(*) c
             FROM order_system.offer_price_change_log
            WHERE run_id=%s AND status='alert'
            GROUP BY alert_type ORDER BY c DESC""",
        (latest[0]["run_id"],),
    )


def _recent_changes(store_key: str, limit: int = 30) -> List[Dict]:
    """Most recent rows that actually hit Mirakl (mirakl_called=1)."""
    return _query(
        """SELECT id, run_id, run_type, shop_sku, warehouse_sku, status,
                  old_origin_price, new_origin_price, old_cost, new_cost,
                  profit_margin_before, profit_margin_after,
                  mirakl_import_id, mirakl_http_status, triggered_at,
                  verify_result
             FROM order_system.offer_price_change_log
            WHERE store_key=%s AND mirakl_called=1
            ORDER BY triggered_at DESC LIMIT %s""",
        (store_key, limit),
    )


def _all_changes(store_key: str, status_filter: Optional[str] = None,
                 limit: int = 200) -> List[Dict]:
    cols = """id, run_id, run_type, shop_sku, warehouse_sku, status,
              alert_type, decision_reason,
              old_origin_price, new_origin_price, old_cost, new_cost,
              profit_margin_before, profit_margin_after,
              mirakl_import_id, mirakl_http_status, triggered_at, verify_result"""
    if status_filter and status_filter != "all":
        return _query(
            f"""SELECT {cols} FROM order_system.offer_price_change_log
                 WHERE store_key=%s AND status=%s
                 ORDER BY triggered_at DESC LIMIT %s""",
            (store_key, status_filter, limit),
        )
    return _query(
        f"""SELECT {cols} FROM order_system.offer_price_change_log
             WHERE store_key=%s
             ORDER BY triggered_at DESC LIMIT %s""",
        (store_key, limit),
    )


# =============================================================================
# Routes - GET
# =============================================================================

@repricing_bp.route("/")
def dashboard():
    store_key = _current_store()
    return render_template(
        "repricing/dashboard.html",
        store_key=store_key,
        stores=store_options(),
        summary=_summary_counts(store_key),
        freshness=get_supplier_freshness(),
        top_candidates=_top_candidates(store_key, 10),
        alert_breakdown=_alert_breakdown_latest_run(store_key),
        recent=_recent_changes(store_key, 20),
    )


@repricing_bp.route("/candidates")
def candidates_page():
    store_key = _current_store()
    return render_template(
        "repricing/candidates.html",
        store_key=store_key,
        stores=store_options(),
        rows=_all_candidates(store_key),
    )


@repricing_bp.route("/alerts")
def alerts_page():
    store_key = _current_store()
    return render_template(
        "repricing/alerts.html",
        store_key=store_key,
        stores=store_options(),
        rows=_alerts(store_key),
        breakdown=_alert_breakdown_latest_run(store_key),
    )


@repricing_bp.route("/changes")
def changes_page():
    store_key = _current_store()
    status = (request.args.get("status") or "all").strip().lower()
    return render_template(
        "repricing/changes.html",
        store_key=store_key,
        stores=store_options(),
        rows=_all_changes(store_key, status_filter=status, limit=300),
        status_filter=status,
    )


@repricing_bp.route("/blacklist")
def blacklist_page():
    store_key = _current_store()
    rows = _query(
        """SELECT shop_sku, failure_count, blacklisted, blacklisted_at,
                  blacklisted_reason, last_alert_type, last_alert_at
             FROM order_system.offer_alert_state
            WHERE store_key=%s AND (blacklisted=1 OR failure_count > 0)
            ORDER BY blacklisted DESC, last_alert_at DESC""",
        (store_key,),
    )
    return render_template(
        "repricing/blacklist.html",
        store_key=store_key,
        stores=store_options(),
        rows=rows,
    )


# =============================================================================
# Routes - POST (single-SKU push, blacklist clear)
# =============================================================================

@repricing_bp.route("/push/<shop_sku>", methods=["POST"])
def push_one(shop_sku):
    """Manual single-SKU push using OF21 (fetch) + OF24 (write).
    Synchronous; blocks for ~67 seconds (OF21 ~2s + OF24 cooldown 65s).
    """
    from app.services.mirakl_offer_api_service import get_offer_by_sku, update_offers
    from scripts.push_single_offer import (  # type: ignore
        build_of24_payload_from_of22,
        build_of24_payload_with_discount,
    )
    from app.services.repricing_monitor_service import _log

    store_key = _current_store()
    scfg = get_store(store_key)
    push_discount = scfg["push_discount"]
    formula_variant = scfg["formula_variant"]

    try:
        offers = fetch_active_offers(store_key)
        ctx = next((o for o in offers if o.shop_sku == shop_sku), None)
        if not ctx:
            return jsonify({"success": False, "msg": "shop_sku not active"}), 400
        if not ctx.warehouse_sku:
            return jsonify({"success": False, "msg": "no warehouse_sku mapping"}), 400

        configs = fetch_pricing_configs(store_key)
        cfg = configs.get(ctx.warehouse_sku)
        if not cfg or cfg.get("return_shipping_base") is None:
            return jsonify({"success": False, "msg": "missing Feishu config or return_shipping_base"}), 400

        supplier = cfg["supplier"]
        sp, _ = lookup_supplier_price(ctx.warehouse_sku, supplier)
        if sp is None:
            return jsonify({"success": False, "msg": "no supplier price"}), 400

        L = float(cfg["length_in"]); W = float(cfg["width_in"])
        H = float(cfg["height_in"]); wt = float(cfg["weight_lb"])
        rb = float(cfg["return_shipping_base"])
        df_override = scfg.get("discount_factor_override")
        if df_override is not None:
            df = float(df_override)
        elif cfg.get("discount_factor") is not None:
            df = float(cfg["discount_factor"])
        else:
            return jsonify({"success": False, "msg": "missing discount_factor"}), 400
        cr = float(cfg["commission_rate"]) if cfg.get("commission_rate") is not None else 0.0

        cost = cost_from_supplier_price(sp, supplier)
        margin = realised_margin(
            current_origin_price=ctx.db_origin_price,
            supplier=supplier, supplier_price=sp,
            return_shipping_base=rb, discount_factor=df, commission_rate=cr,
            length_in=L, width_in=W, height_in=H, weight_lb=wt,
        )
        bd = calculate_breakdown(
            supplier=supplier, supplier_price=sp,
            return_shipping_base=rb, discount_factor=df,
            length_in=L, width_in=W, height_in=H, weight_lb=wt,
            formula_variant=formula_variant,
        )
        target = round(float(bd.origin_price), 2)
        target_discount = round(float(bd.discount_price), 2)

        # Both stores are non_dropship: always OF21 full-fetch + rebuild so
        # every field is preserved (OF24 resets anything not sent).
        full = get_offer_by_sku(store_key, shop_sku)
        if push_discount:
            # Lowes: push 活动前原价 + 折扣后价格 together, reuse discount dates
            payload = build_of24_payload_with_discount(full, target, target_discount)
        else:
            # Macy: push the single `price` (活动前原价)
            payload = build_of24_payload_from_of22(full, target)
        resp = update_offers(store_key, [payload], dry_run=False)

        run_id = f"manual-{shop_sku}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        ok = resp.get("http_status") in (200, 201) and not resp.get("error")

        _log(store_key, run_id, "manual_single", ctx, {
            "supplier": supplier,
            "supplier_price_db": sp,
            "new_cost": round(cost, 4),
            "new_origin_price": target,
            "new_discount_price": target_discount,
            "discount_factor": df, "commission_rate": cr,
            "return_shipping_base": rb,
            "return_shipping_extra": bd.return_shipping_extra,
            "return_cost_estimate": bd.return_cost_estimate,
            "total_cost": round(bd.total_cost, 4),
            "formula_calc_price": round(bd.formula_calc_price, 4),
            "target_origin_price": target,
            "profit_margin_before": round(margin, 4),
            "profit_margin_after": 0.12,
            "mirakl_called": 1,
            "mirakl_import_id": resp.get("import_id"),
            "mirakl_http_status": resp.get("http_status"),
            "mirakl_response_body": resp.get("response_body"),
            "mirakl_request_payload": json.dumps(payload, ensure_ascii=False),
            "ip_used": resp.get("ip_used"),
            "status": "success" if ok else "failed",
            "decision_reason": "manual web push (OF24 HTTP 2xx = success; next-day OF52 cron will catch the rare async-import failure)",
            "error_message": resp.get("error"),
        })

        feishu_writeback = None
        if ok:
            conn = DBManager.get_connection()
            try:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """UPDATE order_system.offerprice_listing
                              SET origin_price=%s,
                                  last_cost_snapshot=%s,
                                  last_cost_snapshot_at=NOW()
                            WHERE shop_sku=%s
                              AND platform=%s AND shop_name=%s""",
                        (target, cost, shop_sku, scfg["platform"], scfg["shop_name"]),
                    )
                conn.commit()
            finally:
                conn.close()

            # Sync latest supplier_price back to Feishu so the Formula
            # cost/profit columns track reality. Best-effort; never blocks
            # the push success.
            try:
                from app.services.feishu_pricing_config_service import write_supplier_prices_to_feishu
                feishu_writeback = write_supplier_prices_to_feishu(
                    [{"warehouse_sku": ctx.warehouse_sku, "supplier_price": sp}],
                    store_key=store_key,
                )
            except Exception as exc:
                feishu_writeback = {"sent": 0, "error": str(exc)}

        return jsonify({
            "success": ok,
            "shop_sku": shop_sku,
            "old_origin_price": float(ctx.db_origin_price),
            "new_origin_price": target,
            "old_margin": round(margin, 4),
            "import_id": resp.get("import_id"),
            "http_status": resp.get("http_status"),
            "ip_used": resp.get("ip_used"),
            "run_id": run_id,
            "feishu_writeback": feishu_writeback,
        })
    except Exception as exc:
        return jsonify({"success": False, "msg": str(exc)}), 500


@repricing_bp.route("/blacklist/<shop_sku>/clear", methods=["POST"])
def blacklist_clear(shop_sku):
    store_key = _current_store()
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """UPDATE order_system.offer_alert_state
                      SET blacklisted=0, blacklisted_at=NULL, blacklisted_reason=NULL,
                          failure_count=0, resolved_at=NOW()
                    WHERE shop_sku=%s AND store_key=%s""",
                (shop_sku, store_key),
            )
        conn.commit()
    finally:
        conn.close()
    flash(f"已解除 {shop_sku} 的黑名单", "success")
    return redirect(url_for("repricing.blacklist_page", store=store_key))


# =============================================================================
# Part 2: full repricing export (xlsx download for manual Mirakl upload)
# =============================================================================

@repricing_bp.route("/full-export", methods=["GET"])
def full_export_page():
    store_key = _current_store()
    # Show most recent runs for THIS store (run_id encodes the store)
    latest = _query(
        """SELECT run_id, MIN(triggered_at) started_at, MAX(triggered_at) finished_at,
                  COUNT(*) total
             FROM order_system.offer_price_change_log
            WHERE run_type='full_export' AND store_key=%s
            GROUP BY run_id
            ORDER BY MAX(triggered_at) DESC
            LIMIT 5""",
        (store_key,),
    )
    return render_template(
        "repricing/full_export.html",
        store_key=store_key,
        stores=store_options(),
        latest_runs=latest,
    )


@repricing_bp.route("/full-export/run", methods=["POST"])
def full_export_run():
    from app.services.repricing_full_export_service import run_full_export
    store_key = _current_store()
    out_dir = os.path.join(
        current_app.config.get("BASE_DIR", current_app.root_path),
        "instance", "exports", "repricing",
    )
    try:
        result = run_full_export(out_dir, store_key=store_key)
    except Exception as exc:
        return jsonify({"success": False, "msg": str(exc)}), 500
    return jsonify(result)


@repricing_bp.route("/full-export/download/<run_id>", methods=["GET"])
def full_export_download(run_id):
    """Download the xlsx generated for a given run_id."""
    from flask import send_file
    store_key = _current_store()
    out_dir = os.path.join(
        current_app.config.get("BASE_DIR", current_app.root_path),
        "instance", "exports", "repricing",
    )
    if not os.path.isdir(out_dir):
        flash("export directory missing", "danger")
        return redirect(url_for("repricing.full_export_page", store=store_key))
    # run_id format: full-<store_key>-YYYYMMDD-HHMMSS-xxxxxx
    # the xlsx filename is <store_key>_repricing_YYYYMMDD_HHMMSS.xlsx
    parts = run_id.split("-")
    timestamp = "_".join(parts[-3:-1]) if len(parts) >= 3 else ""
    candidates = sorted(
        [f for f in os.listdir(out_dir)
         if f.endswith(".xlsx") and timestamp in f]
    )
    if not candidates:
        flash(f"no export file found for {run_id}", "warning")
        return redirect(url_for("repricing.full_export_page", store=store_key))
    return send_file(
        os.path.join(out_dir, candidates[-1]),
        as_attachment=True,
        download_name=candidates[-1],
    )


# =============================================================================
# Batch push: select N candidates -> N OF21 fetches -> 1 batched OF24 call
# =============================================================================

@repricing_bp.route("/push-batch", methods=["POST"])
def push_batch():
    """Push N SKUs in one batched OF24 call.

    Request JSON: {"shop_skus": ["sku1", "sku2", ...]}

    For each sku:
      - Fetch full fields via OF21 (~2s each, no cooldown lock)
      - Compute target price
      - Build OF24 payload preserving all fields

    Then send all payloads in ONE OF24 call (single 65s cooldown).

    Returns per-SKU success/failure breakdown. All-or-nothing wrt the OF24
    call itself: if Mirakl rejects the batch we mark everyone failed.
    """
    from app.services.mirakl_offer_api_service import get_offer_by_sku, update_offers, OF24_DEFAULT_BATCH_SIZE
    from scripts.push_single_offer import (  # type: ignore
        build_of24_payload_from_of22,
        build_of24_payload_with_discount,
    )
    from app.services.repricing_monitor_service import _log

    store_key = _current_store()
    scfg = get_store(store_key)
    push_discount = scfg["push_discount"]
    formula_variant = scfg["formula_variant"]

    payload = request.get_json(silent=True) or {}
    skus_in = payload.get("shop_skus") or []
    if not isinstance(skus_in, list) or not skus_in:
        return jsonify({"success": False, "msg": "shop_skus must be a non-empty list"}), 400
    if len(skus_in) > OF24_DEFAULT_BATCH_SIZE:
        return jsonify({
            "success": False,
            "msg": f"batch too large: {len(skus_in)} > {OF24_DEFAULT_BATCH_SIZE} (chunk on the caller)",
        }), 400

    run_id = f"batch-{store_key}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

    # Pre-load shared context once
    offers = fetch_active_offers(store_key)
    by_sku = {o.shop_sku: o for o in offers}
    configs = fetch_pricing_configs(store_key)

    payloads = []
    rejections = []         # (sku, reason)
    targets_by_sku = {}     # sku -> {target, margin, cfg, supplier, sp, cost, bd}

    for sku in skus_in:
        sku = (sku or "").strip()
        if not sku:
            rejections.append(("", "empty_sku"))
            continue
        ctx = by_sku.get(sku)
        if not ctx:
            rejections.append((sku, "not_active"))
            continue
        if not ctx.warehouse_sku:
            rejections.append((sku, "no_warehouse_sku"))
            continue
        cfg = configs.get(ctx.warehouse_sku)
        if not cfg or cfg.get("return_shipping_base") is None:
            rejections.append((sku, "missing_feishu_config"))
            continue
        supplier = cfg["supplier"]
        if supplier not in ("Costway", "Vevor"):
            rejections.append((sku, f"unsupported_supplier:{supplier}"))
            continue
        sp, _ = lookup_supplier_price(ctx.warehouse_sku, supplier)
        if sp is None:
            rejections.append((sku, "no_supplier_price"))
            continue
        try:
            L = float(cfg["length_in"]); W = float(cfg["width_in"])
            H = float(cfg["height_in"]); wt = float(cfg["weight_lb"])
            rb = float(cfg["return_shipping_base"])
            df_override = scfg.get("discount_factor_override")
            if df_override is not None:
                df = float(df_override)
            elif cfg.get("discount_factor") is not None:
                df = float(cfg["discount_factor"])
            else:
                rejections.append((sku, "missing_discount_factor"))
                continue
            cr = float(cfg["commission_rate"]) if cfg.get("commission_rate") is not None else 0.0
        except (TypeError, ValueError):
            rejections.append((sku, "bad_numeric_cfg"))
            continue

        cost = cost_from_supplier_price(sp, supplier)
        margin = realised_margin(
            current_origin_price=ctx.db_origin_price,
            supplier=supplier, supplier_price=sp,
            return_shipping_base=rb, discount_factor=df, commission_rate=cr,
            length_in=L, width_in=W, height_in=H, weight_lb=wt,
        )
        bd = calculate_breakdown(
            supplier=supplier, supplier_price=sp,
            return_shipping_base=rb, discount_factor=df,
            length_in=L, width_in=W, height_in=H, weight_lb=wt,
            formula_variant=formula_variant,
        )
        target = round(float(bd.origin_price), 2)
        target_discount = round(float(bd.discount_price), 2)
        targets_by_sku[sku] = {
            "ctx": ctx, "target": target, "target_discount": target_discount,
            "margin": margin,
            "supplier": supplier, "sp": sp, "cost": cost, "bd": bd,
            "df": df, "cr": cr, "rb": rb,
        }

    # Build OF24 payloads. Both stores are non_dropship: OF21 full-fetch +
    # rebuild so every field is preserved (OF24 resets anything not sent).
    # push_discount stores (Lowes) also move the 折扣后价格; price-only stores
    # (Macy) move just `price`.
    of21_failures = []
    for sku, info in list(targets_by_sku.items()):
        try:
            full = get_offer_by_sku(store_key, sku)
        except Exception as exc:
            of21_failures.append((sku, str(exc)))
            del targets_by_sku[sku]
            continue
        info["full"] = full
        if push_discount:
            info["payload"] = build_of24_payload_with_discount(
                full, info["target"], info["target_discount"],
            )
        else:
            info["payload"] = build_of24_payload_from_of22(full, info["target"])
        payloads.append(info["payload"])

    if not payloads:
        return jsonify({
            "success": False,
            "run_id": run_id,
            "msg": "no eligible SKUs to push",
            "rejections": rejections,
            "of21_failures": of21_failures,
        }), 400

    # Single OF24 batched call
    resp = update_offers(store_key, payloads, dry_run=False)
    http_status = resp.get("http_status")
    ok = http_status in (200, 201) and not resp.get("error")
    import_id = resp.get("import_id")

    # Persist a log row per pushed SKU + per rejection
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for sku, info in targets_by_sku.items():
        _log(store_key, run_id, "batch_push", info["ctx"], {
            "supplier": info["supplier"],
            "supplier_price_db": info["sp"],
            "new_cost": round(info["cost"], 4),
            "new_origin_price": info["target"],
            "new_discount_price": info["target_discount"],
            "discount_factor": info["df"], "commission_rate": info["cr"],
            "return_shipping_base": info["rb"],
            "return_shipping_extra": info["bd"].return_shipping_extra,
            "return_cost_estimate": info["bd"].return_cost_estimate,
            "total_cost": round(info["bd"].total_cost, 4),
            "formula_calc_price": round(info["bd"].formula_calc_price, 4),
            "target_origin_price": info["target"],
            "profit_margin_before": round(info["margin"], 4),
            "profit_margin_after": 0.12 if ok else None,
            "mirakl_called": 1,
            "mirakl_import_id": import_id,
            "mirakl_http_status": http_status,
            "mirakl_response_body": resp.get("response_body"),
            "mirakl_request_payload": json.dumps(info["payload"], ensure_ascii=False),
            "ip_used": resp.get("ip_used"),
            "status": "success" if ok else "failed",
            "decision_reason": f"batch_push run_id={run_id} (OF24 HTTP 2xx = success)",
            "error_message": resp.get("error"),
        })

    # Update DB origin_price for successful pushes
    feishu_writeback = None
    if ok:
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cursor:
                for sku, info in targets_by_sku.items():
                    cursor.execute(
                        """UPDATE order_system.offerprice_listing
                              SET origin_price=%s,
                                  last_cost_snapshot=%s,
                                  last_cost_snapshot_at=NOW()
                            WHERE shop_sku=%s
                              AND platform=%s AND shop_name=%s""",
                        (info["target"], info["cost"], sku,
                         scfg["platform"], scfg["shop_name"]),
                    )
            conn.commit()
        finally:
            conn.close()

        # Sync supplier prices back to Feishu for the SKUs we just pushed.
        try:
            from app.services.feishu_pricing_config_service import write_supplier_prices_to_feishu
            feishu_writeback = write_supplier_prices_to_feishu(
                [
                    {"warehouse_sku": info["ctx"].warehouse_sku,
                     "supplier_price": info["sp"]}
                    for info in targets_by_sku.values()
                    if info["ctx"].warehouse_sku
                ],
                store_key=store_key,
            )
        except Exception as exc:
            feishu_writeback = {"sent": 0, "error": str(exc)}

    return jsonify({
        "success": ok,
        "run_id": run_id,
        "import_id": import_id,
        "http_status": http_status,
        "pushed": len(targets_by_sku),
        "rejections": rejections,
        "of21_failures": of21_failures,
        "ip_used": resp.get("ip_used"),
        "skus_pushed": list(targets_by_sku.keys()),
        "error_message": resp.get("error"),
        "feishu_writeback": feishu_writeback,
    })


@repricing_bp.route("/full-export/mark-uploaded/<run_id>", methods=["POST"])
def full_export_mark_uploaded(run_id):
    """Operator confirms the xlsx for this run has been uploaded to Mirakl.
    Async writes the supplier_price for every `would_update` SKU in this run
    back to Feishu so the Formula cost/profit columns reflect the now-live
    Mirakl state.

    Returns immediately; the Feishu writeback runs in a background thread
    (full-table scan ~20s) so the HTTP response doesn't block.
    """
    store_key = _current_store()
    rows = _query(
        """SELECT warehouse_sku, supplier_price_db
             FROM order_system.offer_price_change_log
            WHERE run_id=%s
              AND run_type='full_export'
              AND status='dry_run'
              AND warehouse_sku IS NOT NULL
              AND supplier_price_db IS NOT NULL""",
        (run_id,),
    )
    if not rows:
        return jsonify({
            "success": False,
            "msg": f"no would-update rows found for run_id={run_id}",
        }), 400

    updates = [
        {"warehouse_sku": r["warehouse_sku"],
         "supplier_price": float(r["supplier_price_db"])}
        for r in rows
    ]

    def _bg(updates_, store_key_, run_id_):
        try:
            from app.services.feishu_pricing_config_service import write_supplier_prices_to_feishu
            result = write_supplier_prices_to_feishu(updates_, store_key=store_key_)
            print(f"[full_export.mark_uploaded] {run_id_}: {result}")
        except Exception as exc:
            print(f"[full_export.mark_uploaded] {run_id_} exception: {exc}")

    threading.Thread(target=_bg, args=(updates, store_key, run_id), daemon=True).start()

    return jsonify({
        "success": True,
        "run_id": run_id,
        "sku_count": len(updates),
        "msg": "飞书写回已在后台执行（~20-30 秒），可在 gunicorn.log 查结果",
    })


@repricing_bp.route("/full-export/latest-file", methods=["GET"])
def full_export_latest_file():
    """Just download the most recent xlsx without specifying run_id."""
    from flask import send_file
    out_dir = os.path.join(
        current_app.config.get("BASE_DIR", current_app.root_path),
        "instance", "exports", "repricing",
    )
    if not os.path.isdir(out_dir):
        flash("export directory missing", "danger")
        return redirect(url_for("repricing.full_export_page"))
    files = sorted(
        [f for f in os.listdir(out_dir) if f.endswith(".xlsx")],
        reverse=True,
    )
    if not files:
        flash("no export file yet; click 生成 first", "warning")
        return redirect(url_for("repricing.full_export_page"))
    return send_file(
        os.path.join(out_dir, files[0]),
        as_attachment=True,
        download_name=files[0],
    )
