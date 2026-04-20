# ============================================================
# ✅ app/routes/orders.py （你原文件只改 push_tracking 一处）
# ✅ 其它地方完全不动
# ============================================================

import os
import time
from flask import Blueprint, render_template, request, flash, current_app, redirect, jsonify, url_for, session
from werkzeug.utils import secure_filename
import pandas as pd
from app.services.ImportMacyOrder import OrderService
from app.models.db_manager import DBManager
orders_bp = Blueprint('orders', __name__)
from flask import send_file
from app.services.ExportOrder import ExportService
from app.services.tracking_service import (
    upload_tracking_file,
    push_tracking_costway,           # ✅ 保留
    push_tracking_vevor,             # ✅ 保留
    push_tracking_dajian,            # ✅ 保留
    push_tracking_costway_file,      # ✅ 新增
    push_tracking_vevor_file,        # ✅ 新增
    push_tracking_dajian_file,       # ✅ 新增
    export_tracking_file,
    push_tracking_all_platforms_by_text
)
from app.services.mirakl_shipping_service import (
    load_store_config,
    fetch_unshipped_orders,
    build_shipments,
    submit_shipments,
)
from app.services.mirakl_sync_service import (
    DEFAULT_MAX,
    MAX_MAX,
    MAX_MIN,
    get_sync_store_options,
    is_sync_store_supported,
    get_sync_dashboard,
    run_order_sync_job,
    try_acquire_orders_api_cooldown,
)
from app.services.transaction_log_import_service import (
    get_transaction_store_options,
    import_transaction_log_csv,
    list_recent_import_jobs,
)


@orders_bp.route('/import', methods=['GET', 'POST'])
def import_orders():
    if request.method == 'POST':
        platform = (request.form.get('platform') or 'macy').lower().strip()

        file = (
            request.files.get('order_file') or
            request.files.get(f'{platform}_file') or
            request.files.get('macy_file')
        )

        platform_name = {
            'macy': 'Macy',
            'bestbuy': 'BestBuy',
            'walmart': 'Walmart',
            'lowes': 'Lowes',
        }.get(platform, platform)

        if not file or file.filename == '':
            flash(f'请上传 {platform_name} 订单文件', 'warning')
            return redirect(request.url)

        filename = secure_filename(file.filename)
        save_path = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
        file.save(save_path)

        handler_map = {
            'macy': OrderService.process_macy_orders,
            'bestbuy': OrderService.process_bestbuy_orders,
            'walmart': OrderService.process_walmart_orders,
            'lowes': OrderService.process_lowes_orders,
        }

        handler = handler_map.get(platform)
        if not handler:
            flash(f'不支持的平台：{platform_name}', 'danger')
            return redirect(request.url)

        success, msg = handler(save_path)

        if success:
            flash(msg, 'success')
        else:
            flash(f"{platform_name} 导入失败：{msg}", 'danger')

        return redirect(request.url)

    return render_template('order/import.html')


@orders_bp.route('/search', methods=['GET', 'POST'])
def search_orders():
    results = []
    keyword = ""
    if request.method == 'POST':
        keyword = request.form.get('keyword', '').strip()
        if keyword:
            results = DBManager.search_orders(keyword)
            if not results:
                flash(f"未找到包含 '{keyword}' 的订单", 'warning')

    return render_template('order/search.html', results=results, keyword=keyword)


@orders_bp.route("/costway-sku/backfill", methods=["POST"])
def backfill_costway_sku():
    try:
        DBManager.update_costwaymacy_sku()
        DBManager.update_costwaywalmart_sku()
        DBManager.update_costwaybestbuy_sku()
        flash("已补全缺失的 Costway_SKU（Macy/Walmart/Bestbuy）。", "success")
    except Exception as e:
        flash(f"补全失败：{e}", "danger")
    return redirect(request.referrer or "/orders/search")


@orders_bp.route("/mapping", methods=["GET", "POST"])
def mapping_upload():
    if request.method == "POST":
        file = request.files.get("mapping_file")
        if not file or file.filename == "":
            flash("请上传映射表文件（xlsx/csv）", "warning")
            return redirect(request.url)

        filename = secure_filename(file.filename)
        save_path = os.path.join(current_app.config["UPLOAD_FOLDER"], filename)
        file.save(save_path)

        try:
            if filename.lower().endswith(".csv"):
                df = pd.read_csv(save_path, dtype=str)
            else:
                df = pd.read_excel(save_path, dtype=str)
        except Exception as e:
            flash(f"读取文件失败：{e}", "danger")
            return redirect(request.url)

        df.columns = [str(c).strip() for c in df.columns]
        col_map = {c.lower(): c for c in df.columns}
        sku_col = col_map.get("sku")
        wh_col = col_map.get("warehouse_sku") or col_map.get("warehouse sku")

        if not sku_col or not wh_col:
            flash("表头必须包含：SKU, warehouse_SKU", "danger")
            return redirect(request.url)

        df = df[[sku_col, wh_col]].fillna("")
        df[sku_col] = df[sku_col].astype(str).str.strip()
        df[wh_col] = df[wh_col].astype(str).str.strip()
        df = df[(df[sku_col] != "") & (df[wh_col] != "")]

        data = [tuple(x) for x in df[[sku_col, wh_col]].to_numpy()]
        try:
            count = DBManager.upsert_mapping_table(data)
            flash(f"已导入 {count} 条映射关系", "success")
        except Exception as e:
            flash(f"导入失败：{e}", "danger")

        return redirect(request.url)

    return render_template("order/mapping.html")


from flask import Response


@orders_bp.route('/export/haoya-unshipped', methods=['GET'])
def export_haoya_unshipped_xlsx():
    bio, filename = ExportService.export_haoya_unshipped_xlsx()
    return send_file(
        bio,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@orders_bp.route('/export/sishun-unshipped', methods=['GET'])
def export_sishun_unshipped_xlsx():
    bio, filename = ExportService.export_sishun_unshipped_xlsx()
    return send_file(
        bio,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@orders_bp.route('/export/dajian-unshipped', methods=['GET'])
def export_dajian_unshipped_xlsx():
    bio, filename = ExportService.export_dajian_unshipped_xls()
    return send_file(
        bio,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.ms-excel",
    )


@orders_bp.route('/tracking', methods=['GET', 'POST'])
def tracking():
    return render_template("order/tracking.html")


@orders_bp.route("/manual-ship", methods=["GET"])
def manual_ship():
    orders = DBManager.fetch_unshipped_orders_for_manual()
    return render_template("order/manual_ship.html", orders=orders)


@orders_bp.route("/manual-ship/submit", methods=["POST"])
def manual_ship_submit():
    payload = request.get_json(silent=True) or {}
    rows = payload.get("rows") or []
    if not isinstance(rows, list) or not rows:
        return jsonify({"success": False, "msg": "没有可提交的数据"}), 400

    try:
        results = DBManager.bulk_update_tracking_by_costwayorder(rows)
    except Exception as e:
        return jsonify({"success": False, "msg": str(e)}), 500

    stats = {
        "total": len(results),
        "updated": sum(1 for r in results if r.get("ok")),
        "failed": sum(1 for r in results if not r.get("ok")),
    }
    return jsonify({"success": True, "results": results, "stats": stats})


@orders_bp.route("/tracking/upload", methods=["POST"], endpoint="tracking_upload")
def upload():
    file = request.files.get("file")
    if not file:
        return jsonify({"success": False, "msg": "没有上传文件"}), 400

    rows = upload_tracking_file(file)

    return jsonify({
        "success": True,
        "msg": f"上传成功，共读取 {rows} 条",
        "rows": rows
    })


# ✅ ✅ ✅ 这里只改这一段（用 *_file 版本）
@orders_bp.route("/tracking/push/<platform>", methods=["POST"])
def push_tracking(platform):
    file = request.files.get("file")
    if not file:
        return jsonify({"success": False, "msg": "????Excel??"}), 400

    try:
        if platform == "costway":
            result = push_tracking_costway_file(file)
        elif platform == "vevor":
            result = push_tracking_vevor_file(file)
        elif platform == "dajian":
            result = push_tracking_dajian_file(file)
        else:
            return jsonify({"success": False, "msg": "????"}), 400

        return jsonify({"success": True, "data": result})
    except Exception as e:
        return jsonify({"success": False, "msg": str(e)}), 500


@orders_bp.route("/tracking/push-text/<platform>", methods=["POST"])
def push_tracking_text(platform):
    payload = request.get_json(silent=True) or {}
    text = payload.get("text", "")
    if not text.strip():
        return jsonify({"success": False, "msg": "请粘贴订单号和跟踪号"}), 400

    try:
        if platform in ("vevor", "dajian"):
            result = push_tracking_all_platforms_by_text(text)
        else:
            return jsonify({"success": False, "msg": "仅支持 Vevor / 大建"}), 400

        if result.get("error") == "no_rows":
            return jsonify({"success": False, "msg": "没有解析到有效行"}), 400

        return jsonify({"success": True, "data": result})
    except Exception as e:
        return jsonify({"success": False, "msg": str(e)}), 500

@orders_bp.route("/tracking/export/<channel>", methods=["GET"], endpoint="tracking_export")
def export_tracking(channel):
    file_path = export_tracking_file(channel)
    return send_file(file_path, as_attachment=True)


MIRAKL_MACY_STORES = {
    "macy_kuyotq": "Macy-kuyotq",
    "macy_wopet": "Macy-Wopet",
}

MIRAKL_BESTBUY_STORES = {
    "bestbuy_delphi": "Bestbuy-Delphi",
}

MIRAKL_LOWES_STORES = {
    "lowes_autool": "Lowes-Autool",
}

MIRAKL_STORES = {**MIRAKL_MACY_STORES, **MIRAKL_BESTBUY_STORES, **MIRAKL_LOWES_STORES}


@orders_bp.route("/shipping", methods=["GET"])
def mirakl_shipping():
    return render_template(
        "order/mirakl_shipping.html",
        stores=MIRAKL_MACY_STORES,
        page_title="Macy 订单发货",
        page_heading="Macy 订单发货",
        page_subtitle="选择店铺后拉取未发货订单，匹配 Tracking，最后批量发货。",
    )


@orders_bp.route("/shipping/bestbuy", methods=["GET"])
def bestbuy_shipping():
    return render_template(
        "order/mirakl_shipping.html",
        stores=MIRAKL_BESTBUY_STORES,
        page_title="Bestbuy 订单发货",
        page_heading="Bestbuy 订单发货",
        page_subtitle="选择店铺后拉取未发货订单，匹配 Tracking，最后批量发货。",
    )


@orders_bp.route("/shipping/lowes", methods=["GET"])
def lowes_shipping():
    return render_template(
        "order/mirakl_shipping.html",
        stores=MIRAKL_LOWES_STORES,
        page_title="Lowes 订单发货",
        page_heading="Lowes 订单发货",
        page_subtitle="选择店铺后拉取未发货订单，匹配 Tracking，最后批量发货。",
    )


@orders_bp.route("/shipping/preview", methods=["POST"])
def mirakl_shipping_preview():
    store = (request.args.get("store") or "").strip().lower()
    if store not in MIRAKL_STORES:
        return jsonify({"success": False, "msg": "无效店铺"}), 400

    if is_sync_store_supported(store):
        lock_result = try_acquire_orders_api_cooldown(store, action="preview")
        if not lock_result.get("ok"):
            wait_seconds = int(lock_result.get("remaining_seconds") or 0)
            msg = (
                f"/api/orders 冷却中，请等待 {wait_seconds}s "
                f"(last_action={lock_result.get('last_action', '')})"
            )
            return jsonify(
                {
                    "success": False,
                    "msg": msg,
                    "status": "cooldown",
                    "retry_after_seconds": wait_seconds,
                }
            ), 429

    base_dir = current_app.config.get("BASE_DIR", current_app.root_path)
    cfg = load_store_config(base_dir, store)
    if not cfg.get("api_key"):
        return jsonify({"success": False, "msg": f"缺少 API KEY（instance/{store}_key.txt）"}), 400

    try:
        rows = fetch_unshipped_orders(cfg["api_url"], cfg["api_key"], store)
        shipments, stats = build_shipments(rows, store)
        return jsonify({"success": True, "data": shipments, "stats": stats})
    except Exception as e:
        return jsonify({"success": False, "msg": str(e)}), 500


@orders_bp.route("/shipping/submit", methods=["POST"])
def mirakl_shipping_submit():
    store = (request.args.get("store") or "").strip().lower()
    if store not in MIRAKL_STORES:
        return jsonify({"success": False, "msg": "无效店铺"}), 400

    base_dir = current_app.config.get("BASE_DIR", current_app.root_path)
    cfg = load_store_config(base_dir, store)
    if not cfg.get("api_key"):
        return jsonify({"success": False, "msg": f"缺少 API KEY（instance/{store}_key.txt）"}), 400

    payload = request.get_json(silent=True) or {}
    shipments = payload.get("shipments") or []
    batch_size = payload.get("batch_size") or 500
    shipments = [
        s for s in shipments
        if s.get("order_id") and s.get("line_id") and s.get("sku")
    ]
    if not shipments:
        return jsonify({"success": False, "msg": "没有可提交的 shipments"}), 400

    try:
        result = submit_shipments(cfg["api_url"], cfg["api_key"], shipments, batch_size=batch_size, store_key=store)
        failure_csv = result.get("failure_csv")
        if failure_csv:
            filename = os.path.basename(failure_csv)
            result["failure_csv_url"] = f"/orders/shipping/failures/{filename}"
        return jsonify({"success": True, "result": result})
    except Exception as e:
        return jsonify({"success": False, "msg": str(e)}), 500


@orders_bp.route("/shipping/failures/<filename>", methods=["GET"])
def mirakl_failure_csv(filename):
    base_dir = os.path.join(os.getcwd(), "exports", "mirakl_failures")
    file_path = os.path.join(base_dir, filename)
    return send_file(file_path, as_attachment=True)


@orders_bp.route("/sync/macy", methods=["GET"])
def macy_sync_page():
    store_options = get_sync_store_options()
    default_store = next(iter(store_options.keys()), "")
    return render_template(
        "order/macy_sync.html",
        store_key=default_store,
        stores=store_options,
        default_max=DEFAULT_MAX,
        max_min=MAX_MIN,
        max_max=MAX_MAX,
    )


@orders_bp.route("/sync/macy/status", methods=["GET"])
def macy_sync_status():
    store = (request.args.get("store") or "").strip().lower()
    if not store:
        store = next(iter(get_sync_store_options().keys()), "")
    if not is_sync_store_supported(store):
        return jsonify({"success": False, "msg": "invalid sync store"}), 400

    try:
        data = get_sync_dashboard(store, log_limit=30)
        return jsonify({"success": True, "data": data})
    except Exception as e:
        return jsonify({"success": False, "msg": str(e)}), 500


@orders_bp.route("/sync/macy/run", methods=["POST"])
def macy_sync_run_manual():
    payload = request.get_json(silent=True) or {}
    store_key = (payload.get("store_key") or "").strip().lower()
    if not is_sync_store_supported(store_key):
        return jsonify({"success": False, "msg": "invalid sync store"}), 400

    start_time_et = str(payload.get("start_time_et") or "").strip()
    if not start_time_et:
        return jsonify({"success": False, "msg": "start_time_et is required (ET)"}), 400

    max_value = payload.get("max", DEFAULT_MAX)
    try:
        max_value = int(max_value)
    except Exception:
        return jsonify({"success": False, "msg": "max must be an integer"}), 400

    if max_value < MAX_MIN or max_value > MAX_MAX:
        return jsonify({"success": False, "msg": f"max 超出范围 [{MAX_MIN}, {MAX_MAX}]"}), 400

    try:
        result = run_order_sync_job(
            store_key=store_key,
            run_type="manual",
            trigger_source="ui",
            manual_start_time_et=start_time_et,
            max_value=max_value,
        )
        http_status = 200
        if result.get("status") == "skipped":
            http_status = 429
        elif not result.get("success"):
            http_status = 500
        return jsonify(result), http_status
    except Exception as e:
        return jsonify({"success": False, "msg": str(e)}), 500


@orders_bp.route("/transaction-logs", methods=["GET"])
def transaction_logs_page():
    stores = get_transaction_store_options()
    store_key = (request.args.get("store") or "").strip().lower()
    if store_key not in stores:
        store_key = next(iter(stores.keys()), "")

    logs = []
    try:
        logs = list_recent_import_jobs(limit=100)
    except Exception as e:
        flash(f"加载导入日志失败：{e}", "warning")

    return render_template(
        "order/transaction_logs_import.html",
        stores=stores,
        store_key=store_key,
        logs=logs,
    )


@orders_bp.route("/transaction-logs/import", methods=["POST"])
def transaction_logs_import():
    stores = get_transaction_store_options()
    store_key = (request.form.get("store_key") or "").strip().lower()
    if store_key not in stores:
        flash("无效店铺", "danger")
        return redirect(url_for("orders.transaction_logs_page"))

    file = request.files.get("transaction_file")
    if not file or not file.filename:
        flash("请选择交易流水 CSV 文件", "warning")
        return redirect(url_for("orders.transaction_logs_page", store=store_key))

    filename = secure_filename(file.filename)
    if not filename.lower().endswith(".csv"):
        flash("仅支持 CSV 文件", "danger")
        return redirect(url_for("orders.transaction_logs_page", store=store_key))

    upload_dir = current_app.config.get("UPLOAD_FOLDER") or os.path.join(
        current_app.root_path, "..", "instance", "uploads"
    )
    os.makedirs(upload_dir, exist_ok=True)

    saved_name = f"txn_{store_key}_{int(time.time())}_{filename}"
    saved_path = os.path.join(upload_dir, saved_name)
    file.save(saved_path)

    try:
        created_by = (session.get("username") or "").strip()
        stats = import_transaction_log_csv(
            store_key=store_key,
            file_path=saved_path,
            source_filename=filename,
            created_by=created_by,
        )
        flash(
            (
                f"[{stats.store_label}] 导入完成: 新增 {stats.inserted_rows}, "
                f"重复跳过 {stats.duplicate_rows}, 店铺不匹配 {stats.mismatch_rows}, "
                f"错误 {stats.error_rows}, 总行 {stats.total_rows}"
            ),
            "success" if stats.status in ("success", "partial") else "warning",
        )
    except Exception as e:
        flash(f"导入失败：{e}", "danger")
    finally:
        try:
            os.remove(saved_path)
        except Exception:
            pass

    return redirect(url_for("orders.transaction_logs_page", store=store_key))
