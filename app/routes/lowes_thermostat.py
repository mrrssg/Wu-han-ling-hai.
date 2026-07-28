# -*- coding: utf-8 -*-
"""达标恒温器·周体检页面（/thermostat，只读）。"""
import threading

from flask import Blueprint, current_app, jsonify, render_template

from app.models.db_manager import DBManager

lowes_thermostat_bp = Blueprint("lowes_thermostat", __name__)
_RUN = {"running": False}


def _query(sql, params=None):
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params) if params else cur.execute(sql)
            return cur.fetchall() or []
    except Exception as exc:
        if "doesn't exist" in str(exc):
            return []
        raise
    finally:
        conn.close()


@lowes_thermostat_bp.route("/")
def page():
    latest = _query("SELECT MAX(check_date) d FROM order_system.thermostat_weekly")
    check_date = latest[0]["d"] if latest and latest[0]["d"] else None
    rows = _query("""SELECT * FROM order_system.thermostat_weekly
                     WHERE check_date=%s ORDER BY store, operator""",
                  (check_date,)) if check_date else []
    for r in rows:
        r["margin_pct"] = round(float(r["mature_margin"] or 0) * 100, 1)
        r["loss_pct"] = round(float(r["loss_rate"] or 0) * 100, 1)
        r["gap_pct"] = round(float(r["gap"] or 0) * 100, 1)
        r["est_pct"] = round(float(r["est_margin_after"] or 0) * 100, 1)
    n_ok = sum(1 for r in rows if r["verdict"] == "达标")
    n_price = sum(1 for r in rows if "提价" in (r["verdict"] or ""))
    n_delist = sum(1 for r in rows if "下架" in (r["verdict"] or ""))
    return render_template("lowes_thermostat/page.html", rows=rows, check_date=check_date,
                           n_ok=n_ok, n_price=n_price, n_delist=n_delist,
                           running=_RUN["running"])


@lowes_thermostat_bp.route("/run", methods=["POST"])
def run():
    if _RUN["running"]:
        return jsonify({"success": False, "msg": "正在体检中"})
    _RUN["running"] = True
    app_obj = current_app._get_current_object()

    def _bg():
        try:
            import subprocess
            import sys
            from pathlib import Path
            root = Path(app_obj.root_path).parent
            subprocess.run([sys.executable, str(root / "scripts" / "thermostat_weekly.py")],
                           cwd=str(root), timeout=300)
        except Exception as exc:
            print("[thermostat] run failed:", exc)
        finally:
            _RUN["running"] = False

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({"success": True, "msg": "重新体检中，约1分钟后刷新"})
