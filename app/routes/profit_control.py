# -*- coding: utf-8 -*-
"""
利润控制台 Web 路由（Phase 1.5：只读驾驶舱 + 三张行动清单 + CSV导出）。

Pages:
    /profit-control/          总览：KPI + 趋势图 + cell热力矩阵 + 退货损失结构
    /profit-control/issues    问题清单
    /profit-control/actions   行动清单：追款 / 下架候选 / 提价候选
    /profit-control/actions/export?list=recover|delist|raise   CSV 下载
"""
import csv
import io
import json
from datetime import date, timedelta
from typing import Dict, List, Optional

from flask import Blueprint, Response, render_template, request

from app.models.db_manager import DBManager

profit_control_bp = Blueprint("profit_control", __name__)

BASELINE = 0.10


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


def _f(v) -> float:
    return float(v) if v is not None else 0.0


# ---------------------------------------------------------------
# 热力矩阵配色：分歧型 红(低于基线) ↔ 灰(基线) ↔ 蓝(高于基线)
# ---------------------------------------------------------------
_BLUES = ["#f0efec", "#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf"]
_REDS = ["#f0efec", "#f6d9d9", "#eeb0af", "#e58382", "#d03b3b", "#b32e2e"]


def _heat_color(margin: Optional[float]) -> Dict[str, str]:
    if margin is None:
        return {"bg": "#f7f7f5", "ink": "#898781"}
    delta = margin - BASELINE
    steps = min(5, int(abs(delta) / 0.02) + (1 if abs(delta) > 0.002 else 0))
    ramp = _BLUES if delta >= 0 else _REDS
    bg = ramp[steps]
    ink = "#ffffff" if steps >= 4 else "#0b0b0b"
    return {"bg": bg, "ink": ink}


@profit_control_bp.route("/")
def overview():
    snap_date = _latest_snapshot_date()
    cells: List[Dict] = []
    if snap_date:
        cells = _query(
            """SELECT * FROM order_system.profit_cell_daily
               WHERE snapshot_date=%s ORDER BY sale_90d DESC""", (snap_date,))

    # ---- KPI + 环比（对比7天前的滚动30天净利率） ----
    kpi = {"margin_now": None, "margin_prev": None, "sale_90d": 0.0, "net_90d": 0.0,
           "confirmed_loss": 0.0, "pending_exposure": 0.0, "recovery_rate": None,
           "cells_below": 0}
    for c in cells:
        kpi["sale_90d"] += _f(c["sale_90d"])
        kpi["net_90d"] += (_f(c["profit_gross_90d"]) - _f(c["confirmed_return_loss_90d"])
                           - _f(c["expected_pending_loss_90d"]) - _f(c["expected_future_loss_90d"]))
        kpi["confirmed_loss"] += _f(c["confirmed_return_loss_90d"])
        kpi["pending_exposure"] += _f(c["pending_exposure_90d"])
        if not c["meets_baseline"] and _f(c["sale_90d"]) >= 1000:
            kpi["cells_below"] += 1
    kpi["margin_adj"] = (kpi["net_90d"] / kpi["sale_90d"]) if kpi["sale_90d"] > 0 else None

    trow = _query(
        """SELECT stat_date, rolling30_margin FROM order_system.profit_trend_daily
           WHERE scope='公司' AND stat_date IN (%s, %s)""",
        (date.today(), date.today() - timedelta(days=7)))
    for r in trow:
        if r["stat_date"] == date.today():
            kpi["margin_now"] = _f(r["rolling30_margin"]) if r["rolling30_margin"] is not None else None
        else:
            kpi["margin_prev"] = _f(r["rolling30_margin"]) if r["rolling30_margin"] is not None else None

    rec = _query(
        """SELECT SUM(COALESCE(supplier_refund,0)) AS r, SUM(cost) AS c
           FROM order_system.return_case WHERE state='recovered' OR age_days > 90""")
    if rec and rec[0]["c"] and _f(rec[0]["c"]) > 0:
        kpi["recovery_rate"] = _f(rec[0]["r"]) / _f(rec[0]["c"])

    # ---- 趋势序列（近90天：公司 + 各运营 滚动30天净利率；公司每日净贡献柱） ----
    since = date.today() - timedelta(days=90)
    trend_rows = _query(
        """SELECT scope, stat_date, net_1d, rolling30_margin
           FROM order_system.profit_trend_daily
           WHERE stat_date >= %s ORDER BY stat_date""", (since,))
    scopes: Dict[str, Dict[str, list]] = {}
    for r in trend_rows:
        s = scopes.setdefault(r["scope"], {"dates": [], "margin": [], "net": []})
        s["dates"].append(r["stat_date"].strftime("%m-%d"))
        s["margin"].append(round(_f(r["rolling30_margin"]) * 100, 2)
                           if r["rolling30_margin"] is not None else None)
        s["net"].append(round(_f(r["net_1d"]), 0))
    operators = sorted(k for k in scopes.keys() if k not in ("公司", "未分配"))
    trend_json = json.dumps({
        "labels": scopes.get("公司", {}).get("dates", []),
        "company_margin": scopes.get("公司", {}).get("margin", []),
        "company_net": scopes.get("公司", {}).get("net", []),
        "operators": {op: scopes[op]["margin"] for op in operators},
        "baseline": BASELINE * 100,
    }, ensure_ascii=False)

    # ---- 热力矩阵（运营 × 店铺 修正净利率） ----
    ops = sorted(set(c["operator"] for c in cells))
    stores = sorted(set(c["store"] for c in cells),
                    key=lambda s: -sum(_f(c["sale_90d"]) for c in cells if c["store"] == s))
    cell_map = {(c["operator"], c["store"]): c for c in cells}
    matrix = []
    for op in ops:
        row = {"operator": op, "cols": []}
        for st in stores:
            c = cell_map.get((op, st))
            m = float(c["margin_90d_adj"]) if (c and c["margin_90d_adj"] is not None) else None
            row["cols"].append({
                "store": st, "margin": m,
                "sale": _f(c["sale_90d"]) if c else 0.0,
                "gap": _f(c["gap_usd"]) if c else 0.0,
                "color": _heat_color(m),
            })
        matrix.append(row)

    # ---- 退货损失结构（按店铺 stacked） ----
    loss_by_store: Dict[str, Dict[str, float]] = {}
    for c in cells:
        d = loss_by_store.setdefault(c["store"], {"confirmed": 0.0, "pending": 0.0, "future": 0.0})
        d["confirmed"] += _f(c["confirmed_return_loss_90d"])
        d["pending"] += _f(c["expected_pending_loss_90d"])
        d["future"] += _f(c["expected_future_loss_90d"])
    loss_stores = [s for s in stores if s in loss_by_store
                   and sum(loss_by_store[s].values()) > 0]
    loss_json = json.dumps({
        "stores": loss_stores,
        "confirmed": [round(loss_by_store[s]["confirmed"]) for s in loss_stores],
        "pending": [round(loss_by_store[s]["pending"]) for s in loss_stores],
        "future": [round(loss_by_store[s]["future"]) for s in loss_stores],
    }, ensure_ascii=False)

    # ---- 三态 + 新鲜度 ----
    states = {r["state"]: r for r in _query(
        """SELECT state, COUNT(*) AS n, SUM(confirmed_loss) AS loss, SUM(exposure) AS expo
           FROM order_system.return_case
           WHERE return_date > DATE_SUB(CURDATE(), INTERVAL 90 DAY) GROUP BY state""")}
    open_issues = _query(
        """SELECT issue_type, COUNT(*) AS n, SUM(impact_usd) AS impact
           FROM order_system.issue_log WHERE status='open'
           GROUP BY issue_type ORDER BY impact DESC""")
    fresh = {}
    r = _query("SELECT MAX(created_at) AS t FROM order_system.profit_cell_daily")
    fresh["job_last_run"] = r[0]["t"] if r else None
    r = _query("SELECT MAX(sync_time) AS t FROM order_system.macy_order_data")
    fresh["order_sync"] = r[0]["t"] if r else None
    r = _query("SELECT MAX(created_at_db) AS t FROM order_system.mirakl_returns")
    fresh["returns_sync"] = r[0]["t"] if r else None

    return render_template("profit_control/overview.html",
                           snap_date=snap_date, kpi=kpi, matrix=matrix, stores=stores,
                           trend_json=trend_json, loss_json=loss_json,
                           states=states, open_issues=open_issues, fresh=fresh,
                           baseline=BASELINE)


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


# ---------------------------------------------------------------
# 月度影响：退货损失按订单月归因 → 对各月利润/利润率的侵蚀
# ---------------------------------------------------------------

def _store_recovery_rates() -> Dict[str, float]:
    rows = _query(
        """SELECT store, SUM(COALESCE(supplier_refund,0)) AS r, SUM(cost) AS c
           FROM order_system.return_case
           WHERE state='recovered' OR age_days > 90 GROUP BY store""")
    return {x["store"]: (min(1.0, _f(x["r"]) / _f(x["c"])) if _f(x["c"]) > 0 else 0.0)
            for x in rows}


@profit_control_bp.route("/monthly")
def monthly():
    rr = _store_recovery_rates()
    days = 14
    since = date.today() - timedelta(days=days - 1)
    cases = _query(
        """SELECT return_date, operator, store, supplier, state, confirmed_loss, exposure,
                  DATE_FORMAT(order_date, '%%Y-%%m') AS om
           FROM order_system.return_case
           WHERE return_date >= %s AND return_date <= CURDATE()""", (since,))

    def case_loss(c) -> float:
        sup = str(c["supplier"] or "").strip().lower()
        rate = 0.0 if (sup.startswith("vevor") or sup == "司顺") else rr.get(c["store"], 0.0)
        return _f(c["confirmed_loss"]) + _f(c["exposure"]) * (1.0 - rate)

    # 三个维度的每日堆叠：订单月 / 运营 / 店铺
    day_labels = [(since + timedelta(days=i)) for i in range(days)]
    dims = {"month": {}, "op": {}, "store": {}}
    detail: Dict[tuple, float] = {}
    for c in cases:
        loss = case_loss(c)
        if loss <= 0:
            continue
        dkey = c["return_date"].strftime("%Y-%m-%d")
        for dim, key in (("month", c["om"]), ("op", c["operator"]), ("store", c["store"])):
            d = dims[dim].setdefault(key, {})
            d[dkey] = d.get(dkey, 0.0) + loss
        k = (c["return_date"], c["operator"], c["om"])
        detail[k] = detail.get(k, 0.0) + loss

    def series_of(dim_data):
        keys = sorted(dim_data.keys())
        return {"keys": keys,
                "series": {k: [round(dim_data[k].get(d.strftime("%Y-%m-%d"), 0.0), 0)
                               for d in day_labels] for k in keys}}

    chart_json = json.dumps({
        "labels": [d.strftime("%m-%d") for d in day_labels],
        "views": {dim: series_of(data) for dim, data in dims.items()},
    }, ensure_ascii=False)

    # 每日 × 运营 / 每日 × 店铺 透视表（近14天，倒序）
    op_cols = sorted(dims["op"].keys())
    store_cols = sorted(dims["store"].keys(),
                        key=lambda s: -sum(dims["store"][s].values()))
    pivot_days = []
    for d in reversed(day_labels):
        dkey = d.strftime("%Y-%m-%d")
        op_vals = [dims["op"].get(o, {}).get(dkey, 0.0) for o in op_cols]
        st_vals = [dims["store"].get(s, {}).get(dkey, 0.0) for s in store_cols]
        pivot_days.append({"date": dkey, "ops": op_vals, "stores": st_vals,
                           "total": sum(op_vals)})
    pivot_totals = {
        "ops": [sum(dims["op"].get(o, {}).values()) for o in op_cols],
        "stores": [sum(dims["store"].get(s, {}).values()) for s in store_cols],
        "total": sum(sum(v.values()) for v in dims["op"].values()),
    }

    # 昨日速读（最近一个有退货的日期）
    latest = None
    for d in reversed(day_labels):
        dkey = d.strftime("%Y-%m-%d")
        tot = sum(dd.get(dkey, 0.0) for dd in dims["month"].values())
        if tot > 0:
            month_split = sorted(
                [(m, dd.get(dkey, 0.0)) for m, dd in dims["month"].items() if dd.get(dkey, 0)],
                key=lambda x: -x[1])
            latest = {"date": dkey, "total": tot, "split": month_split}
            break

    # cohort：近6个订单月 × 运营（含公司合计行）
    cohort_rows = _query(
        """SELECT * FROM order_system.profit_month_cohort
           WHERE order_month >= DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 5 MONTH), '%Y-%m')
           ORDER BY order_month DESC, sale DESC""")
    months: Dict[str, Dict] = {}
    op_month_sale: Dict[tuple, float] = {}
    for r in cohort_rows:
        m = months.setdefault(r["order_month"], {
            "month": r["order_month"], "sale": 0.0, "profit_gross": 0.0,
            "loss": 0.0, "net": 0.0, "orders": 0, "returns": 0, "ops": []})
        m["sale"] += _f(r["sale"]); m["profit_gross"] += _f(r["profit_gross"])
        m["loss"] += _f(r["loss_expected"]); m["net"] += _f(r["net"])
        m["orders"] += int(r["orders"] or 0); m["returns"] += int(r["returns_cnt"] or 0)
        m["ops"].append(r)
        op_month_sale[(r["operator"], r["order_month"])] = _f(r["sale"])
    month_list = sorted(months.values(), key=lambda x: x["month"], reverse=True)
    for m in month_list:
        m["margin_gross"] = m["profit_gross"] / m["sale"] if m["sale"] > 0 else None
        m["margin_net"] = m["net"] / m["sale"] if m["sale"] > 0 else None
        m["erosion_pp"] = (m["loss"] / m["sale"] * 100) if m["sale"] > 0 else None

    # 归因明细（近7天，含对该运营该订单月的利润率影响）
    detail_rows = []
    for (rd, op, om), loss in detail.items():
        if rd < date.today() - timedelta(days=6):
            continue
        msale = op_month_sale.get((op, om), 0.0)
        detail_rows.append({
            "return_date": rd, "operator": op, "order_month": om,
            "loss": round(loss, 2), "month_sale": msale,
            "impact_pp": (loss / msale * 100) if msale > 0 else None,
        })
    detail_rows.sort(key=lambda x: (x["return_date"], -x["loss"]), reverse=True)

    # 每日合计（给明细表分组头用）
    day_totals: Dict[str, float] = {}
    for r in detail_rows:
        k = r["return_date"].strftime("%Y-%m-%d")
        day_totals[k] = day_totals.get(k, 0.0) + r["loss"]

    return render_template("profit_control/monthly.html",
                           chart_json=chart_json, detail_rows=detail_rows,
                           day_totals=day_totals, month_list=month_list,
                           op_cols=op_cols, store_cols=store_cols,
                           pivot_days=pivot_days, pivot_totals=pivot_totals,
                           latest=latest, baseline=BASELINE)


# ---------------------------------------------------------------
# 行动清单（只读 + CSV 导出）
# ---------------------------------------------------------------

def _recover_list() -> List[Dict]:
    return _query("""
        SELECT order_id, store, operator, supplier, shop_sku, order_date, return_date,
               age_days, cost, exposure
        FROM order_system.return_case
        WHERE state='pending' AND cost >= 20
        ORDER BY exposure DESC LIMIT 500""")


def _delist_list() -> List[Dict]:
    return _query("""
        SELECT shop_sku, store, operator, supplier, orders, sale, profit_gross,
               returns_cnt, loss_expected, net, margin
        FROM order_system.profit_sku_90d
        WHERE returns_cnt >= 2 AND net < -50
        ORDER BY net ASC LIMIT 300""")


def _raise_list() -> List[Dict]:
    rows = _query("""
        SELECT shop_sku, store, operator, supplier, orders, sale, returns_cnt, net, margin
        FROM order_system.profit_sku_90d
        WHERE sale >= 1000 AND orders >= 5 AND margin IS NOT NULL
          AND margin < %s AND net > -50
        ORDER BY sale DESC LIMIT 300""", (BASELINE,))
    for r in rows:
        m = _f(r["margin"])
        # 提价幅度：补足到基线，价格上涨部分还会被抽佣~15%，除以0.7折算
        r["suggest_pct"] = round(min(8.0, max(1.0, (BASELINE - m) * 100 / 0.7)), 1)
    return rows


@profit_control_bp.route("/actions")
def actions():
    recover = _recover_list()
    delist = _delist_list()
    raise_rows = _raise_list()
    totals = {
        "recover_expo": sum(_f(r["exposure"]) for r in recover),
        "delist_net": sum(_f(r["net"]) for r in delist),
        "raise_sale": sum(_f(r["sale"]) for r in raise_rows),
    }
    return render_template("profit_control/actions.html",
                           recover=recover[:100], delist=delist[:100],
                           raise_rows=raise_rows[:100], totals=totals,
                           counts={"recover": len(recover), "delist": len(delist),
                                   "raise": len(raise_rows)})


@profit_control_bp.route("/actions/export")
def actions_export():
    which = request.args.get("list", "recover")
    if which == "recover":
        rows, name = _recover_list(), "追款清单"
    elif which == "delist":
        rows, name = _delist_list(), "下架候选"
    else:
        rows, name = _raise_list(), "提价候选"
    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    data = "﻿" + buf.getvalue()   # BOM 让 Excel 正确识别 UTF-8
    return Response(
        data.encode("utf-8"),
        mimetype="text/csv",
        headers={"Content-Disposition":
                 f"attachment; filename={which}_{date.today()}.csv"})
