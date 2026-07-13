# -*- coding: utf-8 -*-
"""
利润控制台 Web 路由（Phase 1：纯只读）。

Pages:
    /profit-control/          总览：KPI + cell红绿灯矩阵 + 数据新鲜度
    /profit-control/issues    问题清单：规则引擎检出的问题，按金额排序

Phase 1 没有任何写路径（issue 状态流转、行动清单在 Phase 2 加）。
"""
from datetime import date
from typing import Dict, List

from flask import Blueprint, render_template, request

from app.models.db_manager import DBManager

profit_control_bp = Blueprint("profit_control", __name__)


def _query(sql: str, params=None) -> List[Dict]:
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


def _latest_snapshot_date():
    rows = _query("SELECT MAX(snapshot_date) AS d FROM order_system.profit_cell_daily")
    return rows[0]["d"] if rows and rows[0]["d"] else None


@profit_control_bp.route("/")
def overview():
    snap_date = _latest_snapshot_date()
    cells: List[Dict] = []
    kpi = {"sale_90d": 0, "net_profit_90d": 0, "margin_adj": None,
           "confirmed_loss": 0, "pending_exposure": 0, "recovery_rate": None,
           "cells_below": 0}
    if snap_date:
        cells = _query(
            """SELECT * FROM order_system.profit_cell_daily
               WHERE snapshot_date=%s ORDER BY sale_90d DESC""", (snap_date,))
        sale = sum(float(c["sale_90d"] or 0) for c in cells)
        net = sum(float(c["profit_gross_90d"] or 0)
                  - float(c["confirmed_return_loss_90d"] or 0)
                  - float(c["expected_pending_loss_90d"] or 0)
                  - float(c["expected_future_loss_90d"] or 0) for c in cells)
        kpi["sale_90d"] = sale
        kpi["net_profit_90d"] = net
        kpi["margin_adj"] = (net / sale) if sale > 0 else None
        kpi["confirmed_loss"] = sum(float(c["confirmed_return_loss_90d"] or 0) for c in cells)
        kpi["pending_exposure"] = sum(float(c["pending_exposure_90d"] or 0) for c in cells)
        kpi["cells_below"] = sum(1 for c in cells
                                 if not c["meets_baseline"] and float(c["sale_90d"] or 0) >= 1000)

    # 回收率（全局金额口径）
    rec = _query(
        """SELECT SUM(COALESCE(supplier_refund,0)) AS r, SUM(cost) AS c
           FROM order_system.return_case
           WHERE state='recovered' OR age_days > 90""")
    if rec and rec[0]["c"] and float(rec[0]["c"]) > 0:
        kpi["recovery_rate"] = float(rec[0]["r"] or 0) / float(rec[0]["c"])

    # 三态汇总
    states = {r["state"]: r for r in _query(
        """SELECT state, COUNT(*) AS n, SUM(confirmed_loss) AS loss, SUM(exposure) AS expo
           FROM order_system.return_case
           WHERE return_date > DATE_SUB(CURDATE(), INTERVAL 90 DAY)
           GROUP BY state""")}

    # 数据新鲜度
    fresh = {}
    r = _query("SELECT MAX(created_at) AS t FROM order_system.profit_cell_daily")
    fresh["job_last_run"] = r[0]["t"] if r else None
    r = _query("SELECT MAX(sync_time) AS t FROM order_system.macy_order_data")
    fresh["order_sync"] = r[0]["t"] if r else None
    r = _query("SELECT MAX(created_at_db) AS t FROM order_system.mirakl_returns")
    fresh["returns_sync"] = r[0]["t"] if r else None

    return render_template("profit_control/overview.html",
                           snap_date=snap_date, cells=cells, kpi=kpi,
                           states=states, fresh=fresh)


@profit_control_bp.route("/issues")
def issues():
    status = request.args.get("status", "open")
    itype = request.args.get("type", "")
    conds, params = [], []
    if status and status != "all":
        conds.append("status=%s")
        params.append(status)
    if itype:
        conds.append("issue_type=%s")
        params.append(itype)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    rows = _query(
        f"""SELECT * FROM order_system.issue_log {where}
            ORDER BY detected_date DESC, impact_usd DESC LIMIT 500""",
        tuple(params) if params else None)
    type_counts = _query(
        """SELECT issue_type, COUNT(*) AS n, SUM(impact_usd) AS impact
           FROM order_system.issue_log WHERE status='open'
           GROUP BY issue_type ORDER BY impact DESC""")
    return render_template("profit_control/issues.html",
                           rows=rows, type_counts=type_counts,
                           status=status, itype=itype)
