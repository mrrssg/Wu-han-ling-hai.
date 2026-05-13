"""
Web routes for the macy_kuyotq automated repricing system.

Pages:
    /repricing/                  dashboard (overview + KPIs + recent activity)
    /repricing/candidates        SKUs that would trigger Part 1 repricing
    /repricing/alerts            all alert rows (feishu_config_missing etc.)
    /repricing/changes           full change-log history with filters
    /repricing/blacklist         blacklist + per-SKU alert state

APIs:
    POST /repricing/push/<shop_sku>            manual single-SKU push
    POST /repricing/blacklist/<shop_sku>/clear unblock + reset failure count

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


repricing_bp = Blueprint("repricing", __name__)

STORE_KEY = "macy_kuyotq"


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


def _summary_counts() -> Dict[str, int]:
    """Top-level counts for the dashboard."""
    rows = _query(
        """SELECT
              SUM(active=1) AS active_,
              COUNT(*)      AS total
           FROM order_system.offerprice_listing
           WHERE platform='Macy' AND shop_name='kuyotq'"""
    )
    base = rows[0] if rows else {}

    # latest monitor run
    latest = _query(
        """SELECT MAX(run_id) AS run_id, MAX(triggered_at) AS run_at
           FROM order_system.offer_price_change_log
           WHERE run_id LIKE 'mon-macy_kuyotq-%'"""
    )
    latest_run = latest[0] if latest else {}
    latest_run_id = latest_run.get("run_id")

    # counts in that run
    by_status = _query(
        """SELECT status, COUNT(*) c
             FROM order_system.offer_price_change_log
            WHERE run_id = %s
            GROUP BY status""",
        (latest_run_id,),
    ) if latest_run_id else []
    status_map = {r["status"]: int(r["c"]) for r in by_status}

    blk = _query(
        """SELECT COUNT(*) c
             FROM order_system.offer_alert_state
            WHERE store_key=%s AND blacklisted=1""",
        (STORE_KEY,),
    )
    blacklist_n = int(blk[0]["c"]) if blk else 0

    success_today = _query(
        """SELECT COUNT(*) c
             FROM order_system.offer_price_change_log
            WHERE store_key=%s
              AND status IN ('success','pending_verify')
              AND DATE(triggered_at)=CURDATE()""",
        (STORE_KEY,),
    )
    success_today_n = int(success_today[0]["c"]) if success_today else 0

    return {
        "total_offers": int(base.get("total") or 0),
        "active_offers": int(base.get("active_") or 0),
        "latest_run_id": latest_run_id,
        "latest_run_at": latest_run.get("run_at"),
        "latest_skipped": status_map.get("skipped", 0),
        "latest_alert": status_map.get("alert", 0),
        "latest_dry_run": status_map.get("dry_run", 0),
        "blacklist_count": blacklist_n,
        "success_today": success_today_n,
    }


def _top_candidates(limit: int = 10) -> List[Dict]:
    """SKUs that the latest run flagged as dry_run (= would trigger). Sorted by
    margin_before ascending so the most-loss SKUs surface first.
    """
    latest = _query(
        """SELECT run_id FROM order_system.offer_price_change_log
           WHERE run_id LIKE 'mon-macy_kuyotq-%' AND status='dry_run'
           ORDER BY triggered_at DESC LIMIT 1"""
    )
    if not latest:
        return []
    latest_run_id = latest[0]["run_id"]
    return _query(
        """SELECT shop_sku, warehouse_sku, supplier,
                  old_origin_price, new_origin_price, new_cost,
                  profit_margin_before, return_shipping_base, supplier_price_db
             FROM order_system.offer_price_change_log
            WHERE run_id=%s AND status='dry_run'
            ORDER BY profit_margin_before ASC
            LIMIT %s""",
        (latest_run_id, limit),
    )


def _all_candidates() -> List[Dict]:
    """Every dry_run row from the latest run, for the candidates page."""
    latest = _query(
        """SELECT run_id FROM order_system.offer_price_change_log
           WHERE run_id LIKE 'mon-macy_kuyotq-%' AND status='dry_run'
           ORDER BY triggered_at DESC LIMIT 1"""
    )
    if not latest:
        return []
    latest_run_id = latest[0]["run_id"]
    return _query(
        """SELECT * FROM order_system.offer_price_change_log
            WHERE run_id=%s AND status='dry_run'
            ORDER BY profit_margin_before ASC""",
        (latest_run_id,),
    )


def _alerts() -> List[Dict]:
    """Currently unresolved alert state per SKU."""
    return _query(
        """SELECT shop_sku, last_alert_type, last_alert_message, last_alert_at,
                  failure_count, blacklisted, blacklisted_at, blacklisted_reason
             FROM order_system.offer_alert_state
            WHERE store_key=%s AND (resolved_at IS NULL OR last_alert_at > resolved_at)
            ORDER BY last_alert_at DESC""",
        (STORE_KEY,),
    )


def _alert_breakdown_latest_run() -> List[Dict]:
    """alert_type counts from the latest monitor run."""
    latest = _query(
        """SELECT run_id FROM order_system.offer_price_change_log
           WHERE run_id LIKE 'mon-macy_kuyotq-%' AND status='alert'
           ORDER BY triggered_at DESC LIMIT 1"""
    )
    if not latest:
        return []
    return _query(
        """SELECT alert_type, COUNT(*) c
             FROM order_system.offer_price_change_log
            WHERE run_id=%s AND status='alert'
            GROUP BY alert_type
            ORDER BY c DESC""",
        (latest[0]["run_id"],),
    )


def _recent_changes(limit: int = 30) -> List[Dict]:
    """Most recent rows with mirakl_called=1 (actually pushed) or pending_verify."""
    return _query(
        """SELECT id, run_id, run_type, shop_sku, warehouse_sku, status,
                  old_origin_price, new_origin_price, old_cost, new_cost,
                  profit_margin_before, profit_margin_after,
                  mirakl_import_id, mirakl_http_status, triggered_at,
                  verify_result
             FROM order_system.offer_price_change_log
            WHERE store_key=%s
              AND (mirakl_called=1 OR status IN ('success','pending_verify','verification_failed'))
            ORDER BY triggered_at DESC
            LIMIT %s""",
        (STORE_KEY, limit),
    )


def _all_changes(status_filter: Optional[str] = None, limit: int = 200) -> List[Dict]:
    if status_filter and status_filter != "all":
        return _query(
            """SELECT id, run_id, run_type, shop_sku, warehouse_sku, status,
                      alert_type, decision_reason,
                      old_origin_price, new_origin_price, old_cost, new_cost,
                      profit_margin_before, profit_margin_after,
                      mirakl_import_id, mirakl_http_status, triggered_at,
                      verify_result
                 FROM order_system.offer_price_change_log
                WHERE store_key=%s AND status=%s
                ORDER BY triggered_at DESC
                LIMIT %s""",
            (STORE_KEY, status_filter, limit),
        )
    return _query(
        """SELECT id, run_id, run_type, shop_sku, warehouse_sku, status,
                  alert_type, decision_reason,
                  old_origin_price, new_origin_price, old_cost, new_cost,
                  profit_margin_before, profit_margin_after,
                  mirakl_import_id, mirakl_http_status, triggered_at,
                  verify_result
             FROM order_system.offer_price_change_log
            WHERE store_key=%s
            ORDER BY triggered_at DESC
            LIMIT %s""",
        (STORE_KEY, limit),
    )


# =============================================================================
# Routes - GET
# =============================================================================

@repricing_bp.route("/")
def dashboard():
    summary = _summary_counts()
    freshness = get_supplier_freshness()
    top_candidates = _top_candidates(10)
    alert_breakdown = _alert_breakdown_latest_run()
    recent = _recent_changes(20)
    return render_template(
        "repricing/dashboard.html",
        summary=summary,
        freshness=freshness,
        top_candidates=top_candidates,
        alert_breakdown=alert_breakdown,
        recent=recent,
    )


@repricing_bp.route("/candidates")
def candidates_page():
    rows = _all_candidates()
    return render_template(
        "repricing/candidates.html",
        rows=rows,
    )


@repricing_bp.route("/alerts")
def alerts_page():
    rows = _alerts()
    breakdown = _alert_breakdown_latest_run()
    return render_template(
        "repricing/alerts.html",
        rows=rows,
        breakdown=breakdown,
    )


@repricing_bp.route("/changes")
def changes_page():
    status = (request.args.get("status") or "all").strip().lower()
    rows = _all_changes(status_filter=status, limit=300)
    return render_template(
        "repricing/changes.html",
        rows=rows,
        status_filter=status,
    )


@repricing_bp.route("/blacklist")
def blacklist_page():
    rows = _query(
        """SELECT shop_sku, failure_count, blacklisted, blacklisted_at,
                  blacklisted_reason, last_alert_type, last_alert_at
             FROM order_system.offer_alert_state
            WHERE store_key=%s
              AND (blacklisted=1 OR failure_count > 0)
            ORDER BY blacklisted DESC, last_alert_at DESC""",
        (STORE_KEY,),
    )
    return render_template("repricing/blacklist.html", rows=rows)


# =============================================================================
# Routes - POST (single-SKU push, blacklist clear)
# =============================================================================

@repricing_bp.route("/push/<shop_sku>", methods=["POST"])
def push_one(shop_sku):
    """Manual single-SKU push using OF21 (fetch) + OF24 (write).
    Synchronous; blocks for ~67 seconds (OF21 ~2s + OF24 cooldown 65s).
    """
    from app.services.mirakl_offer_api_service import get_offer_by_sku, update_offers
    from scripts.push_single_offer import build_of24_payload_from_of22  # type: ignore
    from app.services.repricing_monitor_service import _log

    try:
        offers = fetch_active_offers(STORE_KEY)
        ctx = next((o for o in offers if o.shop_sku == shop_sku), None)
        if not ctx:
            return jsonify({"success": False, "msg": "shop_sku not active"}), 400
        if not ctx.warehouse_sku:
            return jsonify({"success": False, "msg": "no warehouse_sku mapping"}), 400

        configs = fetch_pricing_configs(STORE_KEY)
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
        df = float(cfg["discount_factor"])
        cr = float(cfg["commission_rate"])

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
        )
        target = round(float(bd.origin_price), 2)

        full = get_offer_by_sku(STORE_KEY, shop_sku)
        payload = build_of24_payload_from_of22(full, target)
        resp = update_offers(STORE_KEY, [payload], dry_run=False)

        run_id = f"manual-{shop_sku}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        ok = resp.get("http_status") in (200, 201) and not resp.get("error")

        _log(STORE_KEY, run_id, "manual_single", ctx, {
            "supplier": supplier,
            "supplier_price_db": sp,
            "new_cost": round(cost, 4),
            "new_origin_price": target,
            "new_discount_price": round(target * df, 2),
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
            "ip_used": resp.get("ip_used"),
            "status": "pending_verify" if ok else "failed",
            "decision_reason": "manual web push",
            "error_message": resp.get("error"),
        })

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
                              AND platform='Macy' AND shop_name='kuyotq'""",
                        (target, cost, shop_sku),
                    )
                conn.commit()
            finally:
                conn.close()

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
        })
    except Exception as exc:
        return jsonify({"success": False, "msg": str(exc)}), 500


@repricing_bp.route("/blacklist/<shop_sku>/clear", methods=["POST"])
def blacklist_clear(shop_sku):
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """UPDATE order_system.offer_alert_state
                      SET blacklisted=0, blacklisted_at=NULL, blacklisted_reason=NULL,
                          failure_count=0, resolved_at=NOW()
                    WHERE shop_sku=%s""",
                (shop_sku,),
            )
        conn.commit()
    finally:
        conn.close()
    flash(f"已解除 {shop_sku} 的黑名单", "success")
    return redirect(url_for("repricing.blacklist_page"))


# =============================================================================
# Part 2: full repricing export (xlsx download for manual Mirakl upload)
# =============================================================================

@repricing_bp.route("/full-export", methods=["GET"])
def full_export_page():
    # Show most recent run + download link
    latest = _query(
        """SELECT run_id, MIN(triggered_at) started_at, MAX(triggered_at) finished_at,
                  COUNT(*) total
             FROM order_system.offer_price_change_log
            WHERE run_type='full_export'
            GROUP BY run_id
            ORDER BY MAX(triggered_at) DESC
            LIMIT 5"""
    )
    return render_template("repricing/full_export.html", latest_runs=latest)


@repricing_bp.route("/full-export/run", methods=["POST"])
def full_export_run():
    from app.services.repricing_full_export_service import run_full_export
    out_dir = os.path.join(
        current_app.config.get("BASE_DIR", current_app.root_path),
        "instance", "exports", "repricing",
    )
    try:
        result = run_full_export(out_dir)
    except Exception as exc:
        return jsonify({"success": False, "msg": str(exc)}), 500
    return jsonify(result)


@repricing_bp.route("/full-export/download/<run_id>", methods=["GET"])
def full_export_download(run_id):
    """Download the xlsx generated for a given run_id."""
    from flask import send_file
    out_dir = os.path.join(
        current_app.config.get("BASE_DIR", current_app.root_path),
        "instance", "exports", "repricing",
    )
    # Find the file matching this run_id - filename has the timestamp embedded
    # Easier: scan the dir for files mentioning the run_id's timestamp
    if not os.path.isdir(out_dir):
        flash("export directory missing", "danger")
        return redirect(url_for("repricing.full_export_page"))
    # run_id format: full-macy_kuyotq-YYYYMMDD-HHMMSS-xxxxxx
    parts = run_id.split("-")
    timestamp = "_".join(parts[2:4]) if len(parts) >= 4 else ""
    candidates = sorted(
        [f for f in os.listdir(out_dir)
         if f.endswith(".xlsx") and timestamp in f.replace("-", "_")]
    )
    if not candidates:
        flash(f"no export file found for {run_id}", "warning")
        return redirect(url_for("repricing.full_export_page"))
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
    from scripts.push_single_offer import build_of24_payload_from_of22  # type: ignore
    from app.services.repricing_monitor_service import _log

    payload = request.get_json(silent=True) or {}
    skus_in = payload.get("shop_skus") or []
    if not isinstance(skus_in, list) or not skus_in:
        return jsonify({"success": False, "msg": "shop_skus must be a non-empty list"}), 400
    if len(skus_in) > OF24_DEFAULT_BATCH_SIZE:
        return jsonify({
            "success": False,
            "msg": f"batch too large: {len(skus_in)} > {OF24_DEFAULT_BATCH_SIZE} (chunk on the caller)",
        }), 400

    run_id = f"batch-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

    # Pre-load shared context once
    offers = fetch_active_offers(STORE_KEY)
    by_sku = {o.shop_sku: o for o in offers}
    configs = fetch_pricing_configs(STORE_KEY)

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
            df = float(cfg["discount_factor"])
            cr = float(cfg["commission_rate"])
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
        )
        target = round(float(bd.origin_price), 2)
        targets_by_sku[sku] = {
            "ctx": ctx, "target": target, "margin": margin,
            "supplier": supplier, "sp": sp, "cost": cost, "bd": bd,
            "df": df, "cr": cr, "rb": rb,
        }

    # Now OF21 each sku in order to build payloads.
    # OF21 is not rate-capped; we fetch sequentially to be polite (~2s each).
    of21_failures = []
    for sku, info in list(targets_by_sku.items()):
        try:
            full = get_offer_by_sku(STORE_KEY, sku)
        except Exception as exc:
            of21_failures.append((sku, str(exc)))
            del targets_by_sku[sku]
            continue
        info["full"] = full
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
    resp = update_offers(STORE_KEY, payloads, dry_run=False)
    http_status = resp.get("http_status")
    ok = http_status in (200, 201) and not resp.get("error")
    import_id = resp.get("import_id")

    # Persist a log row per pushed SKU + per rejection
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for sku, info in targets_by_sku.items():
        _log(STORE_KEY, run_id, "batch_push", info["ctx"], {
            "supplier": info["supplier"],
            "supplier_price_db": info["sp"],
            "new_cost": round(info["cost"], 4),
            "new_origin_price": info["target"],
            "new_discount_price": round(info["target"] * info["df"], 2),
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
            "ip_used": resp.get("ip_used"),
            "status": "pending_verify" if ok else "failed",
            "decision_reason": f"batch_push run_id={run_id}",
            "error_message": resp.get("error"),
        })

    # Update DB origin_price for successful pushes
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
                              AND platform='Macy' AND shop_name='kuyotq'""",
                        (info["target"], info["cost"], sku),
                    )
            conn.commit()
        finally:
            conn.close()

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
