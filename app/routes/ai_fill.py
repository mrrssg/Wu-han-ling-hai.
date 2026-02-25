import json
import os
from flask import Blueprint, current_app, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

from app.services.ai_fill_service import (
    get_instance_dir,
    load_config,
    save_config,
    start_job,
)

ai_fill_bp = Blueprint("ai_fill", __name__)


@ai_fill_bp.route("/", methods=["GET"])
def index():
    base_dir = current_app.config["BASE_DIR"]
    cfg = load_config(base_dir)

    whitelist_text = "\n".join(cfg.get("whitelist", []))
    fixed_values_text = json.dumps(cfg.get("fixed_values", {}), ensure_ascii=False, indent=2)
    max_options_threshold = cfg.get("max_options_threshold", 60)

    return render_template(
        "ai_fill/index.html",
        whitelist_text=whitelist_text,
        fixed_values_text=fixed_values_text,
        max_options_threshold=max_options_threshold,
    )


@ai_fill_bp.route("/start", methods=["POST"])
def start():
    base_dir = current_app.config["BASE_DIR"]
    instance_dir = get_instance_dir(base_dir)
    uploads_dir = os.path.join(instance_dir, "uploads")
    outputs_dir = os.path.join(instance_dir, "outputs")
    jobs_dir = os.path.join(instance_dir, "jobs")
    os.makedirs(uploads_dir, exist_ok=True)
    os.makedirs(outputs_dir, exist_ok=True)
    os.makedirs(jobs_dir, exist_ok=True)

    source_file = request.files.get("source_file")
    template_file = request.files.get("template_file")
    if not source_file or not template_file:
        return jsonify({"ok": False, "error": "Missing source_file or template_file"}), 400

    whitelist_text = request.form.get("whitelist", "")
    fixed_values_text = request.form.get("fixed_values", "")
    max_options_threshold_raw = request.form.get("max_options_threshold", "60")

    whitelist = [line.strip() for line in whitelist_text.splitlines() if line.strip()]

    try:
        fixed_values = json.loads(fixed_values_text) if fixed_values_text.strip() else {}
        if not isinstance(fixed_values, dict):
            raise ValueError("fixed_values must be a JSON object")
    except Exception as e:
        return jsonify({"ok": False, "error": f"Invalid fixed_values JSON: {e}"}), 400

    try:
        max_options_threshold = int(max_options_threshold_raw)
    except Exception:
        max_options_threshold = 60

    cfg = {
        "whitelist": whitelist,
        "fixed_values": fixed_values,
        "max_options_threshold": max_options_threshold,
    }
    save_config(base_dir, cfg)

    source_name = secure_filename(source_file.filename)
    template_name = secure_filename(template_file.filename)
    source_path = os.path.join(uploads_dir, source_name)
    template_path = os.path.join(uploads_dir, template_name)
    source_file.save(source_path)
    template_file.save(template_path)

    job_id = os.urandom(8).hex()
    output_path = os.path.join(outputs_dir, f"hd_ai_filled_{job_id}.xlsx")
    progress_path = os.path.join(jobs_dir, f"{job_id}.json")

    start_job(
        base_dir=base_dir,
        source_path=source_path,
        template_path=template_path,
        output_path=output_path,
        config=cfg,
        progress_path=progress_path,
        job_id=job_id,
    )

    return jsonify({"ok": True, "job_id": job_id})


@ai_fill_bp.route("/progress/<job_id>", methods=["GET"])
def progress(job_id: str):
    base_dir = current_app.config["BASE_DIR"]
    jobs_dir = os.path.join(get_instance_dir(base_dir), "jobs")
    progress_path = os.path.join(jobs_dir, f"{job_id}.json")

    if not os.path.exists(progress_path):
        return jsonify({"ok": False, "error": "Job not found"}), 404

    with open(progress_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    return jsonify({"ok": True, "data": data})


@ai_fill_bp.route("/download/<job_id>", methods=["GET"])
def download(job_id: str):
    base_dir = current_app.config["BASE_DIR"]
    jobs_dir = os.path.join(get_instance_dir(base_dir), "jobs")
    progress_path = os.path.join(jobs_dir, f"{job_id}.json")

    if not os.path.exists(progress_path):
        return jsonify({"ok": False, "error": "Job not found"}), 404

    with open(progress_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if data.get("status") != "done":
        return jsonify({"ok": False, "error": "Job not completed"}), 400

    output_path = data.get("output_file")
    if not output_path or not os.path.exists(output_path):
        return jsonify({"ok": False, "error": "Output file missing"}), 404

    return send_file(output_path, as_attachment=True, download_name=os.path.basename(output_path))
