"""
Flask routes for the HD Vevor shipping page (Teapplix label purchase).

GET  /hd-shipping/                       index page (Vevor-only filtered list)
POST /hd-shipping/refresh                JSON: re-pull unshipped orders + enrich
POST /hd-shipping/label/<txn_id>         JSON: purchase a single label
POST /hd-shipping/labels/batch           JSON: purchase up to BATCH_LIMIT labels
POST /hd-shipping/label/<txn_id>/cancel  JSON: cancel a previously purchased label
GET  /hd-shipping/label/<txn_id>         JSON: read persisted record (post-mortem)
GET  /hd-shipping/history                page: list past hd_label_records
"""
import logging
from flask import Blueprint, jsonify, render_template, request, Response, abort

from app.models.db_manager import DBManager
from app.services import teapplix_label_service as tp
from app.services import vevor_label_workflow as wf


log = logging.getLogger(__name__)

hd_shipping_bp = Blueprint("hd_shipping", __name__)


def _fetchall(sql, params=None):
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            if params:
                cur.execute(sql, params)
            else:
                cur.execute(sql)
            return cur.fetchall() or []
    finally:
        conn.close()


@hd_shipping_bp.route("/", methods=["GET"])
def index():
    rows = []
    error = None
    try:
        orders = tp.list_unshipped_orders()
        rows = wf.enrich_orders(orders)
    except Exception as e:
        log.exception("Failed to load Teapplix orders")
        error = str(e)
    # Vevor-only by default; show all if ?all=1 (debug)
    show_all = request.args.get("all") == "1"
    if not show_all:
        rows = [r for r in rows if r.get("is_vevor")]

    summary = {
        "total": len(rows),
        "eligible": sum(1 for r in rows if r.get("is_eligible")),
        "missing_dims": sum(1 for r in rows if r.get("is_vevor") and not r.get("has_dims")),
        "out_of_stock": sum(1 for r in rows
                            if r.get("is_vevor")
                            and not (r.get("stock_w10") or r.get("stock_w432"))),
        "already_shipped": sum(1 for r in rows if r.get("existing_status") == "success"),
    }
    return render_template(
        "hd_shipping/index.html",
        rows=rows, summary=summary, error=error, show_all=show_all,
        batch_limit=wf.BATCH_LIMIT,
    )


@hd_shipping_bp.route("/refresh", methods=["POST"])
def refresh():
    """Re-pull current unshipped orders and return enriched rows as JSON."""
    try:
        orders = tp.list_unshipped_orders()
        rows = wf.enrich_orders(orders)
    except Exception as e:
        log.exception("refresh failed")
        return jsonify({"ok": False, "error": str(e)}), 500
    show_all = request.args.get("all") == "1"
    if not show_all:
        rows = [r for r in rows if r.get("is_vevor")]
    return jsonify({"ok": True, "rows": rows})


@hd_shipping_bp.route("/label/<path:txn_id>", methods=["POST"])
def purchase_single(txn_id):
    try:
        result = wf.purchase_one(txn_id)
    except Exception as e:
        log.exception("purchase_one failed")
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify(result)


@hd_shipping_bp.route("/labels/batch", methods=["POST"])
def purchase_batch():
    payload = request.get_json(silent=True) or {}
    txn_ids = payload.get("txn_ids") or []
    if not isinstance(txn_ids, list):
        return jsonify({"ok": False, "error": "txn_ids must be an array"}), 400
    if not txn_ids:
        return jsonify({"ok": False, "error": "txn_ids is empty"}), 400
    try:
        result = wf.purchase_many(txn_ids)
    except Exception as e:
        log.exception("purchase_many failed")
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify(result)


@hd_shipping_bp.route("/label/<path:txn_id>/cancel", methods=["POST"])
def cancel(txn_id):
    payload = request.get_json(silent=True) or {}
    reason = (payload.get("reason") or "").strip()
    force = bool(payload.get("force"))
    try:
        result = wf.cancel_one(txn_id, reason=reason, force=force)
    except Exception as e:
        log.exception("cancel failed")
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify(result)


@hd_shipping_bp.route("/label/<path:txn_id>", methods=["GET"])
def get_label(txn_id):
    rec = wf.get_existing_record(txn_id)
    if not rec:
        return jsonify({"ok": False, "error": "not_found"}), 404
    # JSON serialise datetime → str
    safe = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in rec.items()}
    return jsonify({"ok": True, "record": safe})


@hd_shipping_bp.route("/history", methods=["GET"])
def history():
    rows = _fetchall(
        "SELECT txn_id, store_key, shop_sku, warehouse_sku, profile_id, "
        "       warehouse_id, tracking_number, label_url, postage, status, "
        "       error_msg, excel_row_tsv, created_at, cancelled_at "
        "  FROM hd_label_records "
        " ORDER BY created_at DESC LIMIT 500"
    )
    return render_template("hd_shipping/history.html", rows=rows)


@hd_shipping_bp.route("/label/<path:txn_id>/pdf", methods=["GET"])
def download_pdf(txn_id):
    """Server-side proxy for Teapplix LabelData URLs. The Teapplix download
    endpoint requires the APIToken header, which the browser can't supply
    directly, so we relay the request and stream the bytes back."""
    rec = wf.get_existing_record(txn_id)
    if not rec or not rec.get("label_url"):
        abort(404)
    status, payload, ctype = tp.download_label(rec["label_url"])
    if status != 200:
        return Response(payload, status=status, mimetype=ctype or "text/plain")
    resp = Response(payload, mimetype=ctype or "application/pdf")
    # Inline so the browser previews the PDF in a new tab
    resp.headers["Content-Disposition"] = (
        f'inline; filename="label-{txn_id}.pdf"'
    )
    return resp
