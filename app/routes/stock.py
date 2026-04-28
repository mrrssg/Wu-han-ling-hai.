import os
from flask import Blueprint, render_template, request, flash, redirect, url_for, send_file, current_app
from werkzeug.utils import secure_filename
from app.models.db_manager import DBManager
from app.services.stock_service import StockService


stock_bp = Blueprint('stock', __name__)


@stock_bp.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        platform = request.form.get('platform')
        file = request.files.get('stock_file')

        if not platform or not file:
            flash('Please provide platform and file.', 'warning')
        else:
            filename = secure_filename(file.filename)
            upload_path = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
            output_filename = f"result_{filename}"
            output_path = os.path.join(current_app.config['UPLOAD_FOLDER'], output_filename)

            file.save(upload_path)
            if platform == 'bestbuy':
                success, msg = StockService.process_bestbuy_stock(upload_path, output_path, current_app.config['BASE_DIR'])
                if success:
                    flash(f'Bestbuy processed: {msg}', 'success')
                    return send_file(output_path, as_attachment=True, download_name=output_filename)
                else:
                    flash(f'Bestbuy failed: {msg}', 'danger')
            elif platform == 'macy':
                success, msg = StockService.process_macy_stock(upload_path, output_path, current_app.config['BASE_DIR'])
                if success:
                    flash(f'Macy processed: {msg}', 'success')
                    return send_file(output_path, as_attachment=True, download_name=output_filename)
                else:
                    flash(f'Macy failed: {msg}', 'danger')
            elif platform == 'lowes':
                success, msg = StockService.process_lowes_stock(upload_path, output_path, current_app.config['BASE_DIR'])
                if success:
                    flash(f'Lowes processed: {msg}', 'success')
                    return send_file(output_path, as_attachment=True, download_name=output_filename)
                else:
                    flash(f'Lowes failed: {msg}', 'danger')
            elif platform == 'walmart':
                flash('Walmart not implemented.', 'info')

    last_update_time = DBManager.get_last_stock_update_time()
    cfg = StockService.load_hd_config(current_app.config['BASE_DIR'])
    excluded_text = "\n".join(cfg.get('excluded', []))

    supplier_rules = cfg.get("supplier_rules", {}) if isinstance(cfg, dict) else {}
    return render_template(
        'stock/index.html',
        last_update_time=last_update_time,
        hd_excluded_text=excluded_text,
        hd_threshold=cfg.get('threshold', 50),
        hd_qty_high=cfg.get('qty_high', 20),
        hd_qty_low=cfg.get('qty_low', 0),
        supplier_rules=supplier_rules,
        costway_zip_password=cfg.get('costway_zip_password', ''),
        hd_log=None,
        hd_message=None,
        hd_success=None,
    )


@stock_bp.route('/sync-suppliers', methods=['POST'])
def sync_suppliers():
    try:
        success, message = StockService.sync_all_suppliers()
        if success:
            flash(f'Sync success: {message}', 'success')
        else:
            flash(f'Sync partial or failed: {message}', 'warning')
    except Exception as e:
        flash(f'System error: {str(e)}', 'danger')

    return redirect(url_for('stock.index'))


@stock_bp.route('/hd-sync', methods=['POST'])
def sync_hd():
    base_dir = current_app.config['BASE_DIR']
    excluded_text = request.form.get('hd_excluded', '')
    excluded_list = [line.strip() for line in excluded_text.splitlines() if line.strip()]

    def _to_int(val, default):
        try:
            return int(val)
        except Exception:
            return default

    cfg = StockService.load_hd_config(base_dir)
    threshold = _to_int(cfg.get('threshold', 50), 50)
    qty_high = _to_int(cfg.get('qty_high', 20), 20)
    qty_low = _to_int(cfg.get('qty_low', 0), 0)
    supplier_rules = cfg.get("supplier_rules", {}) if isinstance(cfg, dict) else {}
    StockService.save_hd_config(base_dir, excluded_list, threshold, qty_high, qty_low, supplier_rules)

    success, message, log_rows = StockService.sync_hd_inventory(
        base_dir, excluded_list, threshold, qty_high, qty_low
    )
    last_update_time = DBManager.get_last_stock_update_time()

    return render_template(
        'stock/index.html',
        last_update_time=last_update_time,
        hd_excluded_text=excluded_text,
        hd_threshold=threshold,
        hd_qty_high=qty_high,
        hd_qty_low=qty_low,
        supplier_rules=supplier_rules,
        hd_log=log_rows,
        hd_message=message,
        hd_success=success,
    )


@stock_bp.route('/hd-save', methods=['POST'])
def save_hd_excluded():
    base_dir = current_app.config['BASE_DIR']
    excluded_text = request.form.get('hd_excluded', '')
    excluded_list = [line.strip() for line in excluded_text.splitlines() if line.strip()]

    def _to_int(val, default):
        try:
            return int(val)
        except Exception:
            return default

    cfg = StockService.load_hd_config(base_dir)
    threshold = _to_int(cfg.get('threshold', 50), 50)
    qty_high = _to_int(cfg.get('qty_high', 20), 20)
    qty_low = _to_int(cfg.get('qty_low', 0), 0)
    supplier_rules = cfg.get("supplier_rules", {}) if isinstance(cfg, dict) else {}
    StockService.save_hd_config(base_dir, excluded_list, threshold, qty_high, qty_low, supplier_rules)
    flash('排除名单已保存。', 'success')
    return redirect(url_for('stock.index'))


@stock_bp.route('/hd-save-rules', methods=['POST'])
def save_hd_rules():
    base_dir = current_app.config['BASE_DIR']
    cfg = StockService.load_hd_config(base_dir)
    excluded_list = cfg.get("excluded", []) if isinstance(cfg, dict) else []

    def _to_int(val, default):
        try:
            return int(val)
        except Exception:
            return default

    threshold = _to_int(cfg.get('threshold', 50), 50)
    qty_high = _to_int(cfg.get('qty_high', 20), 20)
    qty_low = _to_int(cfg.get('qty_low', 0), 0)

    supplier_rules = _parse_supplier_rules(request)
    StockService.save_hd_config(base_dir, excluded_list, threshold, qty_high, qty_low, supplier_rules)
    flash('规则已保存。', 'success')
    return redirect(url_for('stock.index'))


@stock_bp.route('/upload-hyl', methods=['POST'])
def upload_hyl():
    file = request.files.get('hyl_file')
    if not file or not file.filename:
        flash('请选择 HYL 库存文件 (.xlsx)', 'warning')
        return redirect(url_for('stock.index'))

    filename = secure_filename(file.filename)
    upload_path = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
    file.save(upload_path)

    try:
        success, msg = StockService.process_hyl_data(upload_path)
        if success:
            flash(f'HYL 库存更新成功: {msg}', 'success')
        else:
            flash(f'HYL 库存更新失败: {msg}', 'danger')
    except Exception as e:
        flash(f'HYL 系统异常: {str(e)}', 'danger')

    return redirect(url_for('stock.index'))


@stock_bp.route('/save-costway-pwd', methods=['POST'])
def save_costway_pwd():
    base_dir = current_app.config['BASE_DIR']
    pwd = request.form.get('costway_zip_password', '').strip()
    cfg = StockService.load_hd_config(base_dir)
    StockService.save_hd_config(
        base_dir,
        cfg.get('excluded', []),
        cfg.get('threshold', 50),
        cfg.get('qty_high', 20),
        cfg.get('qty_low', 0),
        cfg.get('supplier_rules', {}),
        costway_zip_password=pwd,
    )
    flash(f'Costway 解压密码已保存。', 'success')
    return redirect(url_for('stock.index'))


def _parse_supplier_rules(req):
    def _to_int(val, default):
        try:
            return int(val)
        except Exception:
            return default

    keys = ["haoya", "sishun", "dajian", "songmics", "hyl"]
    rules = {}
    for key in keys:
        rules[key] = {
            "threshold": _to_int(req.form.get(f"rule_{key}_threshold"), 50),
            "qty_high": _to_int(req.form.get(f"rule_{key}_qty_high"), 20),
            "qty_low": _to_int(req.form.get(f"rule_{key}_qty_low"), 0),
        }
    return rules
