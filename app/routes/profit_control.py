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

from flask import (Blueprint, Response, current_app, jsonify, redirect,
                   render_template, request, url_for)

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

    # Costway回收率：Kuyotq已到期实测（司顺不参与——政策不退，混进来会拉低均值）
    rec = _query(
        """SELECT SUM(COALESCE(supplier_refund,0)) AS r, SUM(cost) AS c
           FROM order_system.return_case
           WHERE state <> 'not_charged' AND (state='recovered' OR age_days > 90)
             AND store='Macys-Kuyotq' AND supplier='Costway'""")
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
# Listing哨兵：退货触发的 我方listing vs 供应商listing 对比结果
# ---------------------------------------------------------------

@profit_control_bp.route("/sentinel")
def sentinel():
    verdict = request.args.get("verdict", "")
    status = request.args.get("status", "open")
    conds, params = [], []
    if verdict:
        conds.append("verdict=%s")
        params.append(verdict)
    if status and status != "all":
        conds.append("status=%s")
        params.append(status)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    rows = _query(
        f"""SELECT * FROM order_system.listing_sentinel_findings {where}
            ORDER BY FIELD(verdict,'severe','minor','clean'), returns_recent DESC,
                     audit_date DESC LIMIT 200""",
        tuple(params) if params else None)
    for r in rows:
        try:
            r["issues"] = json.loads(r["issues_json"] or "[]")
        except Exception:
            r["issues"] = []
        try:
            r["fix"] = json.loads(r["fix_json"]) if r.get("fix_json") else None
        except Exception:
            r["fix"] = None
    counts = {c["k"]: int(c["n"]) for c in _query(
        """SELECT CONCAT(verdict,'-',status) AS k, COUNT(*) AS n
           FROM order_system.listing_sentinel_findings GROUP BY verdict, status""")}
    return render_template("profit_control/sentinel.html",
                           rows=rows, verdict=verdict, status=status, counts=counts)


@profit_control_bp.route("/sentinel/fix", methods=["POST"])
def sentinel_fix():
    data = request.get_json(silent=True) or {}
    fid = int(data.get("id") or 0)
    force = bool(data.get("force"))
    if not fid:
        return jsonify({"ok": False, "msg": "missing id"}), 400
    from app.services.listing_sentinel_service import generate_fix
    try:
        result = generate_fix(current_app.config.get("BASE_DIR")
                              or str(__import__("pathlib").Path(__file__).resolve().parents[2]),
                              fid, force=force)
    except Exception as exc:
        result = {"ok": False, "msg": str(exc)}
    return jsonify(result)


@profit_control_bp.route("/sentinel/mark", methods=["POST"])
def sentinel_mark():
    data = request.get_json(silent=True) or {}
    fid = int(data.get("id") or 0)
    new_status = (data.get("status") or "").strip()
    if not fid or new_status not in ("fixed", "false_positive", "open"):
        return jsonify({"ok": False}), 400
    _exec("UPDATE order_system.listing_sentinel_findings SET status=%s WHERE id=%s",
          (new_status, fid))
    return jsonify({"ok": True})


# ---------------------------------------------------------------
# 月度诊断：为什么没达基线 → 谁 → 缺口拆解 → 量化处方 → 标记已做
# ---------------------------------------------------------------

def _rx_done_map(month: str) -> Dict[str, Dict]:
    rows = _query("""SELECT id, target, created_at FROM order_system.action_log
                     WHERE action_type='rx_done' AND status='executed'
                       AND target LIKE %s""", (f"{month}|%",))
    return {r["target"]: r for r in rows}


@profit_control_bp.route("/diagnose")
def diagnose():
    month = (request.args.get("month") or "")[:7]
    operator = (request.args.get("operator") or "").strip()
    store = (request.args.get("store") or "").strip()
    if not month:
        return redirect(url_for("profit_control.monthly"))

    cells = _query("""SELECT * FROM order_system.profit_month_cohort
                      WHERE order_month=%s ORDER BY sale DESC""", (month,))
    m_sale = sum(_f(c["sale"]) for c in cells)
    m_net = sum(_f(c["net"]) for c in cells)
    m_summary = {
        "sale": m_sale, "net": m_net,
        "margin": m_net / m_sale if m_sale > 0 else None,
        "target": m_sale * BASELINE,
        "gap": max(0.0, m_sale * BASELINE - m_net),
    }
    for c in cells:
        sale = _f(c["sale"])
        c["gap"] = max(0.0, sale * BASELINE - _f(c["net"]))
        c["ok"] = _f(c["net"]) >= sale * BASELINE
    fail_cells = sorted([c for c in cells if not c["ok"] and _f(c["sale"]) >= 500],
                        key=lambda x: -x["gap"])

    detail = None
    if operator and store:
        row = next((c for c in cells if c["operator"] == operator and c["store"] == store), None)
        if row:
            sale = _f(row["sale"])
            gross = _f(row["profit_gross"])
            loss = _f(row["loss_expected"])
            target = sale * BASELINE
            # 退货损失分解（该cell该月的确认退货）
            parts = _query("""
                SELECT supplier,
                  SUM(CASE WHEN state='recovered' THEN GREATEST(cost-COALESCE(supplier_refund,0),0)+return_fee ELSE 0 END) AS l_recovered,
                  SUM(CASE WHEN state IN ('pending','written_off') AND supplier='Costway'
                            AND DATEDIFF(CURDATE(), order_date) <= 90 THEN exposure ELSE 0 END) AS expo_in,
                  SUM(CASE WHEN state IN ('pending','written_off') AND supplier='Costway'
                            AND DATEDIFF(CURDATE(), order_date) > 90 THEN exposure+confirmed_loss ELSE 0 END) AS l_expired,
                  SUM(CASE WHEN state IN ('pending','written_off') AND supplier<>'Costway'
                           THEN exposure+confirmed_loss ELSE 0 END) AS l_norefund,
                  SUM(CASE WHEN state='pending' THEN return_fee ELSE 0 END) AS l_fee
                FROM order_system.return_case
                WHERE DATE_FORMAT(order_date,'%%Y-%%m')=%s AND operator=%s AND store=%s
                  AND state <> 'not_charged'
                GROUP BY supplier""", (month, operator, store))
            rr = _recovery_rates_pairs()
            from app.services.profit_control_service import _recovery_rate
            loss_parts = {"sishun": 0.0, "haoya_in": 0.0, "haoya_in_recoverable": 0.0,
                          "haoya_expired": 0.0, "fee": 0.0, "recovered": 0.0}
            for p in parts:
                sup = str(p["supplier"] or "")
                loss_parts["recovered"] += _f(p["l_recovered"])
                loss_parts["fee"] += _f(p["l_fee"])
                if sup == "Costway":
                    expo_in = _f(p["expo_in"])
                    rate = _recovery_rate(rr, store, "Costway")
                    loss_parts["haoya_in"] += expo_in * (1 - rate)
                    loss_parts["haoya_in_recoverable"] += expo_in * rate
                    loss_parts["haoya_expired"] += _f(p["l_expired"])
                else:
                    loss_parts["sishun"] += _f(p["l_norefund"]) + _f(p["l_expired"]) + _f(p["expo_in"])
            neg_profit = _f(row["neg_profit"])
            gross_gap = max(0.0, target - gross)   # 就算一分退货没有,毛利也不够到基线的部分

            # 该店铺的处方素材（90天口径）
            delist_rows = _query("""
                SELECT shop_sku, orders, sale, returns_cnt, loss_expected, net
                FROM order_system.profit_sku_90d
                WHERE store=%s AND operator=%s AND returns_cnt>=2 AND net<-50
                  AND NOT EXISTS (SELECT 1 FROM order_system.action_log a
                    WHERE a.action_type='delist' AND a.status='executed'
                      AND a.target=CONCAT(profit_sku_90d.shop_sku,'@',profit_sku_90d.store))
                ORDER BY net ASC LIMIT 10""", (store, operator))
            raise_rows = _query("""
                SELECT shop_sku, orders, sale, margin FROM order_system.profit_sku_90d
                WHERE store=%s AND operator=%s AND sale>=1000 AND orders>=5
                  AND margin IS NOT NULL AND margin < %s AND net > -50
                ORDER BY sale DESC LIMIT 10""",
                (store, operator, BASELINE))
            raise_uplift = sum(_f(r["sale"]) * (BASELINE - _f(r["margin"])) for r in raise_rows)
            # 追款处方只算「本诊断月」的订单（和上方缺口拆解同口径）；跨月总量另行提示
            recover_in_window = _query("""
                SELECT COUNT(*) n, COALESCE(SUM(exposure),0) expo FROM order_system.return_case
                WHERE store=%s AND operator=%s AND state='pending' AND supplier='Costway'
                  AND DATE_FORMAT(order_date,'%%Y-%%m') = %s
                  AND DATEDIFF(CURDATE(), order_date) <= 90""", (store, operator, month))[0]
            recover_all_months = _query("""
                SELECT COUNT(*) n, COALESCE(SUM(exposure),0) expo FROM order_system.return_case
                WHERE store=%s AND operator=%s AND state='pending' AND supplier='Costway'
                  AND DATEDIFF(CURDATE(), order_date) <= 90""", (store, operator))[0]
            recover_rows = _query("""
                SELECT order_id, shop_sku, return_date,
                       90 - DATEDIFF(CURDATE(), order_date) AS days_left, exposure
                FROM order_system.return_case
                WHERE store=%s AND operator=%s AND state='pending' AND supplier='Costway'
                  AND DATE_FORMAT(order_date,'%%Y-%%m') = %s
                  AND DATEDIFF(CURDATE(), order_date) <= 90
                ORDER BY days_left ASC, exposure DESC LIMIT 30""", (store, operator, month))
            sishun_rows = _query("""
                SELECT shop_sku, COUNT(*) AS n, ROUND(SUM(exposure + confirmed_loss)) AS loss
                FROM order_system.return_case
                WHERE store=%s AND operator=%s AND supplier <> 'Costway'
                  AND state IN ('pending','written_off')
                  AND DATE_FORMAT(order_date,'%%Y-%%m') = %s
                GROUP BY shop_sku ORDER BY loss DESC LIMIT 15""", (store, operator, month))

            tbl_recover = {"head": ["订单号", "SKU", "退货日", "剩余追款天数", "货值$"],
                "rows": [[r["order_id"], r["shop_sku"], str(r["return_date"]),
                          f"{r['days_left']}天", f"{_f(r['exposure']):,.0f}"] for r in recover_rows],
                "more": f"共{recover_in_window['n']}笔，这里显示最急的30笔；全量在 行动清单→追款清单"}
            tbl_delist = {"head": ["SKU", "90D单数", "退货", "销售$", "退货期望损失$", "净贡献$"],
                "rows": [[r["shop_sku"], r["orders"], r["returns_cnt"],
                          f"{_f(r['sale']):,.0f}", f"{_f(r['loss_expected']):,.0f}",
                          f"{_f(r['net']):,.0f}"] for r in delist_rows],
                "more": "去 行动清单→下架候选 操作并点「已下架」"}
            tbl_raise = {"head": ["SKU", "90D单数", "销售$", "净利率", "建议提价"],
                "rows": [[r["shop_sku"], r["orders"], f"{_f(r['sale']):,.0f}",
                          f"{_f(r['margin'])*100:.1f}%",
                          f"+{min(8.0, max(1.0, (BASELINE - _f(r['margin'])) * 100 / 0.7)):.1f}%"]
                         for r in raise_rows],
                "more": "全量与CSV在 行动清单→提价候选"}
            tbl_sishun = {"head": ["SKU", "退货笔数", "损失$"],
                "rows": [[r["shop_sku"], r["n"], f"{_f(r['loss']):,.0f}"] for r in sishun_rows],
                "more": "司顺退货全损——这些SKU优先评估提价/换源/下架"}
            neg_rows = _query("""
                SELECT order_id, shop_sku, order_date, sale, cost, profit, is_actual
                FROM order_system.profit_neg_orders
                WHERE order_month=%s AND operator=%s AND store=%s
                ORDER BY profit ASC LIMIT 20""", (month, operator, store))
            tbl_neg = {"head": ["订单号", "SKU", "下单日", "售价$", "成本$", "利润$", "口径"],
                "rows": [[r["order_id"], r["shop_sku"], str(r["order_date"]),
                          f"{_f(r['sale']):,.2f}", f"{_f(r['cost']):,.2f}",
                          f"{_f(r['profit']):,.2f}",
                          "实际" if r["is_actual"] else "预估"] for r in neg_rows],
                "more": "利润为负但没退货的单——核对售价是否低于成本线、活动折扣是否打穿"}

            rx = []
            expo_in_w = _f(recover_in_window["expo"])
            cross_note = ""
            if _f(recover_all_months["expo"]) > expo_in_w + 1:
                cross_note = (f"（该cell其他月份还挂着{int(recover_all_months['n']) - int(recover_in_window['n'])}笔、"
                              f"${_f(recover_all_months['expo']) - expo_in_w:,.0f}在窗口内，去行动清单→追款清单看全量）")
            if expo_in_w > 50:
                rate_cw = _recovery_rate(rr, store, "Costway")
                mon_label = f"{int(month[5:])}月"
                if rate_cw > 0:
                    rx.append({"key": "recover", "title": f"追{mon_label}订单的豪雅退款（见效最快）",
                        "amount": expo_in_w * rate_cw, "table": tbl_recover,
                        "why": f"{mon_label}卖出、已退货的订单里还有{recover_in_window['n']}笔豪雅退款没回，"
                               f"货值${expo_in_w:,.0f}、都在90天追款窗口内——按回收率{rate_cw*100:.0f}%"
                               f"预计能收回${expo_in_w * rate_cw:,.0f}，回填后直接改善{mon_label}净利{cross_note}",
                        "how": "行动清单→追款清单（按店铺筛），逐笔找豪雅对账"})
                else:
                    rx.append({"key": "recover", "title": f"试追{mon_label}订单的豪雅退款（能否退回未知，窗口只有90天）",
                        "amount": expo_in_w, "table": tbl_recover,
                        "why": f"{mon_label}卖出、已退货的订单里有{recover_in_window['n']}笔豪雅退款没回、"
                               f"货值敞口${expo_in_w:,.0f}、都还在90天窗口内。{store}至今没有退款回填记录，"
                               f"能不能要回不确定——但超窗就彻底追不了了，值得逐笔去谈{cross_note}",
                        "how": "行动清单→追款清单（按店铺筛），逐笔找豪雅对账，结果记备注"})
            if delist_rows:
                stop = -sum(_f(r["net"]) for r in delist_rows)
                rx.append({"key": "delist",
                    "title": f"下架{len(delist_rows)}个负期望SKU（止血·近90天口径,不只{int(month[5:])}月）",
                    "amount": stop, "table": tbl_delist,
                    "why": f"{store}该运营有{len(delist_rows)}个SKU退货≥2且净贡献为负（近90天合计−${stop:,.0f}，"
                           f"单月样本太小易误判所以用90天证据）——继续卖只会继续亏，下架不影响有效销量",
                    "how": "行动清单→下架候选（本店铺的），店铺后台下架后点「已下架」"})
            if raise_uplift > 50:
                rx.append({"key": "raise",
                    "title": f"提价低利润SKU（可持续改善·近90天口径,不只{int(month[5:])}月）",
                    "amount": raise_uplift, "table": tbl_raise,
                    "why": f"{store}该运营有{len(raise_rows)}个SKU销量正常但净利率低于10%——"
                           f"按建议幅度提到基线，近90天口径可增利≈${raise_uplift:,.0f}（动作影响的是未来所有月份）",
                    "how": "行动清单→提价候选，导出后走改价流程"})
            if loss_parts["sishun"] > 200:
                rx.append({"key": "sishun", "title": "司顺退货是纯损失（结构性）",
                    "amount": loss_parts["sishun"], "table": tbl_sishun,
                    "why": f"该cell当月司顺退货损失${loss_parts['sishun']:,.0f}且一分收不回——"
                           f"司顺高退货率SKU要么提价覆盖风险，要么换豪雅货源/下架",
                    "how": "从下架/提价候选里优先处理司顺SKU"})
            if neg_profit < -100:
                rx.append({"key": "negsale", "title": "亏本卖的正常单（查定价）",
                    "amount": -neg_profit, "table": tbl_neg,
                    "why": f"当月有{int(row['neg_n'] or 0)}单没退货也亏钱（合计−${-neg_profit:,.0f}）——"
                           f"通常是定价低于成本线或活动折扣打穿了",
                    "how": "展开下面的具体订单，核对售价与价格公式；批量问题走改价系统"})

            done = _rx_done_map(month)
            for x in rx:
                key = f"{month}|{operator}|{store}|{x['key']}"
                x["target_key"] = key
                d = done.get(key)
                x["done"] = bool(d)
                x["done_at"] = d["created_at"] if d else None
                x["done_id"] = d["id"] if d else None

            detail = {"row": row, "sale": sale, "gross": gross, "loss": loss,
                      "target": target, "gap": max(0.0, target - (gross - loss)),
                      "gross_gap": gross_gap, "neg_profit": neg_profit,
                      "loss_parts": loss_parts, "rx": rx}

    return render_template("profit_control/diagnose.html",
                           month=month, m_label=f"{int(month[5:])}月",
                           summary=m_summary, cells=cells, fail_cells=fail_cells,
                           operator=operator, store=store, detail=detail,
                           baseline=BASELINE)


@profit_control_bp.route("/diagnose/mark", methods=["POST"])
def diagnose_mark():
    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip()[:120]
    if not key or key.count("|") != 3:
        return jsonify({"ok": False, "msg": "bad key"}), 400
    exists = _query("""SELECT id FROM order_system.action_log
                       WHERE action_type='rx_done' AND status='executed' AND target=%s""", (key,))
    if not exists:
        _exec("""INSERT INTO order_system.action_log (action_type, target, store, status)
                 VALUES ('rx_done', %s, %s, 'executed')""", (key, key.split("|")[2]))
    return jsonify({"ok": True})


@profit_control_bp.route("/diagnose/unmark", methods=["POST"])
def diagnose_unmark():
    data = request.get_json(silent=True) or {}
    aid = int(data.get("id") or 0)
    if not aid:
        return jsonify({"ok": False}), 400
    _exec("UPDATE order_system.action_log SET status='cancelled' WHERE id=%s AND action_type='rx_done'",
          (aid,))
    return jsonify({"ok": True})


# ---------------------------------------------------------------
# 月度影响：退货损失按订单月归因 → 对各月利润/利润率的侵蚀
# ---------------------------------------------------------------

def _recovery_rates_pairs() -> Dict:
    """(店铺,供应商) 粒度的已到期实测回收率 + 店铺级兜底，格式与 service 的 rates 一致，
    直接配合 profit_control_service._recovery_rate 使用（含 司顺=0 / Macy-Costway≥90% 规则）。"""
    rows = _query(
        """SELECT store, supplier, COUNT(*) AS n,
                  SUM(COALESCE(supplier_refund,0)) AS r, SUM(cost) AS c
           FROM order_system.return_case
           WHERE state <> 'not_charged' AND (state='recovered' OR age_days > 90)
           GROUP BY store, supplier""")
    rates: Dict = {}
    store_agg: Dict[str, list] = {}
    for x in rows:
        rsum, csum = _f(x["r"]), _f(x["c"])
        agg = store_agg.setdefault(x["store"], [0.0, 0.0])
        agg[0] += rsum
        agg[1] += csum
        if int(x["n"] or 0) >= 10 and csum > 0:
            rates[(x["store"], x["supplier"])] = min(1.0, rsum / csum)
    rates["__store__"] = {s: (min(1.0, a[0] / a[1]) if a[1] > 0 else 0.0)
                          for s, a in store_agg.items()}
    return rates


@profit_control_bp.route("/monthly")
def monthly():
    from app.services.profit_control_service import _recovery_rate
    rr = _recovery_rates_pairs()
    days = 14
    since = date.today() - timedelta(days=days - 1)
    cases = _query(
        """SELECT return_date, operator, store, supplier, state, confirmed_loss, exposure,
                  DATE_FORMAT(order_date, '%%Y-%%m') AS om
           FROM order_system.return_case
           WHERE return_date >= %s AND return_date <= CURDATE()""", (since,))

    def case_loss(c) -> float:
        rate = _recovery_rate(rr, c["store"], c["supplier"])
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
    for r in cohort_rows:   # 现为 月×运营×店铺 粒度，此处聚合到月/运营
        m = months.setdefault(r["order_month"], {
            "month": r["order_month"], "sale": 0.0, "profit_gross": 0.0,
            "loss": 0.0, "net": 0.0, "loss_actual": 0.0, "net_actual": 0.0,
            "orders": 0, "returns": 0, "op_agg": {}})
        m["sale"] += _f(r["sale"]); m["profit_gross"] += _f(r["profit_gross"])
        m["loss"] += _f(r["loss_expected"]); m["net"] += _f(r["net"])
        m["loss_actual"] += _f(r["loss_actual"]); m["net_actual"] += _f(r["net_actual"])
        m["gross_est"] = m.get("gross_est", 0.0) + _f(r.get("gross_est"))
        m["orders"] += int(r["orders"] or 0); m["returns"] += int(r["returns_cnt"] or 0)
        od = m["op_agg"].setdefault(r["operator"], {"net": 0.0, "loss": 0.0})
        od["net"] += _f(r["net"]); od["loss"] += _f(r["loss_expected"])
        k = (r["operator"], r["order_month"])
        op_month_sale[k] = op_month_sale.get(k, 0.0) + _f(r["sale"])
    month_list = sorted(months.values(), key=lambda x: x["month"], reverse=True)
    for m in month_list:
        m["margin_gross"] = m["profit_gross"] / m["sale"] if m["sale"] > 0 else None
        m["margin_net"] = m["net"] / m["sale"] if m["sale"] > 0 else None
        m["margin_net_actual"] = m["net_actual"] / m["sale"] if m["sale"] > 0 else None
        m["hanging"] = m["loss_actual"] - m["loss"]   # 挂在供应商那边、按规则预计能退回的钱
        m["erosion_pp"] = (m["loss"] / m["sale"] * 100) if m["sale"] > 0 else None

    # 近7天：每天的退货记到哪个月的账上，逐条写成"账本变化"：
    # 扣$X ⇒ 该运营该月净利 before→after、净利率 before→after（以该月最新账为基准）
    om_op: Dict[tuple, list] = {}
    for r in cohort_rows:   # 店铺级行聚合到 运营×月
        k = (r["operator"], r["order_month"])
        cur = om_op.setdefault(k, [0.0, 0.0])
        cur[0] += _f(r["net"]); cur[1] += _f(r["sale"])
    day_blocks = []
    for d in reversed(day_labels[-7:]):
        dkey = d.strftime("%Y-%m-%d")
        month_map: Dict[str, Dict[str, float]] = {}
        for (rd, op, om), loss in detail.items():
            if rd.strftime("%Y-%m-%d") != dkey:
                continue
            mm = month_map.setdefault(om, {})
            mm[op] = mm.get(op, 0.0) + loss
        if not month_map:
            continue
        months_list = []
        for om in sorted(month_map.keys(), key=lambda m: -sum(month_map[m].values())):
            op_lines = []
            for op, loss in sorted(month_map[om].items(), key=lambda x: -x[1]):
                net_sale = om_op.get((op, om))
                line = {"op": op, "loss": round(loss, 0)}
                if net_sale and net_sale[1] > 0:
                    after, sale = net_sale
                    before = after + loss
                    line.update({
                        "before": round(before, 0), "after": round(after, 0),
                        "m_before": round(before / sale * 100, 2),
                        "m_after": round(after / sale * 100, 2),
                    })
                op_lines.append(line)
            months_list.append({
                "label": f"{int(om[5:])}月",
                "loss": sum(month_map[om].values()),
                "ops": op_lines,
            })
        day_blocks.append({"date": dkey,
                           "total": sum(m["loss"] for m in months_list),
                           "months": months_list})

    # 每月"未记全的退货"数量 → 活账本按钮
    # 判定：账单未导入(income_actual IS NULL) 或 实际利润仍>0(退货后果没体现在账上)。
    # 注意不能只看"实际到账>1"——部分退款的单到账留有余额但亏损已入账(实际利润为负)，不算未记全。
    # 未定案的退货标记单：账单未导入(暂按退货计) 或 账单未扣款(已按正常单计)。
    # 用户规则：有「供应商退款」的一律视为已闭环，不进清单。
    unbooked_counts = {r["m"]: int(r["n"]) for r in _query(
        """SELECT DATE_FORMAT(order_date,'%Y-%m') AS m, COUNT(*) AS n
           FROM order_system.return_case
           WHERE COALESCE(supplier_refund, 0) <= 0
             AND (income_actual IS NULL OR state = 'not_charged')
           GROUP BY DATE_FORMAT(order_date,'%Y-%m')""")}

    # 每月活账本：条形图宽度 + 白话行
    for m in month_list:
        m["unbooked_n"] = unbooked_counts.get(m["month"], 0)
        gross = m["profit_gross"]
        if gross > 0:
            net_w = max(0.0, m["net"]) / gross * 100
            m["net_w"] = round(min(100.0, net_w), 1)
            m["loss_w"] = round(100.0 - m["net_w"], 1)
        else:
            m["net_w"], m["loss_w"] = 0.0, 100.0
        m["label"] = f"{int(m['month'][5:])}月"
        m["ops_line"] = " ｜ ".join(
            f"{op} 预估净利${d['net']:,.0f}（被扣${d['loss']:,.0f}）"
            for op, d in sorted(m["op_agg"].items(), key=lambda x: -x[1]["net"]))

    return render_template("profit_control/monthly.html",
                           chart_json=chart_json, day_blocks=day_blocks,
                           month_list=month_list,
                           op_cols=op_cols, store_cols=store_cols,
                           pivot_days=pivot_days, pivot_totals=pivot_totals,
                           latest=latest, baseline=BASELINE)


@profit_control_bp.route("/monthly/unbooked")
def monthly_unbooked():
    """某订单月里有退货标记、但账单还没定案的订单明细。
    ⚪ 账单未导入(income_actual IS NULL)：暂按退货预判、计期望损失；
    🟢 账单未扣款(state='not_charged')：买家货款没被扣回 → 不算退货，已按正常单计利润。
    有「供应商退款」的一律视为已闭环，不出现（用户规则）。"""
    month = (request.args.get("month") or "")[:7]
    rows = _query(
        """SELECT order_id, store, operator, supplier, shop_sku, order_date, return_date,
                  age_days, sale, cost, income_actual, profit_actual, supplier_refund,
                  return_fee, state
           FROM order_system.return_case
           WHERE DATE_FORMAT(order_date,'%%Y-%%m') = %s
             AND COALESCE(supplier_refund, 0) <= 0
             AND (income_actual IS NULL OR state = 'not_charged')
           ORDER BY income_actual DESC, cost DESC""", (month,))
    for r in rows:
        r["status"] = "账单未导入" if r["income_actual"] is None else "账单未扣款"
    totals = {
        "n": len(rows),
        "sale": sum(_f(r["sale"]) for r in rows),
        "cost": sum(_f(r["cost"]) for r in rows),
        "income": sum(_f(r["income_actual"]) for r in rows),
        "unbooked_n": sum(1 for r in rows if r["income_actual"] is not None),
    }
    if request.args.get("format") == "csv":
        buf = io.StringIO()
        if rows:
            writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        return Response(("﻿" + buf.getvalue()).encode("utf-8"), mimetype="text/csv",
                        headers={"Content-Disposition":
                                 f"attachment; filename=unbooked_returns_{month}.csv"})
    return render_template("profit_control/unbooked.html",
                           month=month, rows=rows, totals=totals)


# ---------------------------------------------------------------
# 行动清单（只读 + CSV 导出）
# ---------------------------------------------------------------

def _recover_list() -> List[Dict]:
    """追款清单：只有豪雅(Costway)可追款，窗口=下单90天内（用户规则 2026-07-14）。
    按剩余天数升序（快过期的排前面），同天数按敞口降序。"""
    return _query("""
        SELECT id, order_id, store, operator, shop_sku, order_date, return_date,
               age_days, cost, exposure, recover_note,
               90 - DATEDIFF(CURDATE(), order_date) AS days_left
        FROM order_system.return_case
        WHERE state='pending' AND supplier='Costway' AND cost >= 20
          AND DATEDIFF(CURDATE(), order_date) <= 90
        ORDER BY days_left ASC, exposure DESC LIMIT 500""")


def _recover_expired_stats() -> Dict:
    rows = _query("""
        SELECT COUNT(*) AS n, COALESCE(SUM(exposure),0) AS expo
        FROM order_system.return_case
        WHERE state='pending' AND supplier='Costway' AND cost >= 20
          AND DATEDIFF(CURDATE(), order_date) > 90""")
    return rows[0] if rows else {"n": 0, "expo": 0}


def _recover_expired_list() -> List[Dict]:
    return _query("""
        SELECT id, order_id, store, operator, shop_sku, order_date, return_date,
               cost, exposure, recover_note,
               DATEDIFF(CURDATE(), order_date) - 90 AS days_over
        FROM order_system.return_case
        WHERE state='pending' AND supplier='Costway' AND cost >= 20
          AND DATEDIFF(CURDATE(), order_date) > 90
        ORDER BY exposure DESC LIMIT 500""")


def _delist_list() -> List[Dict]:
    """下架候选：排除已被人工标记"已下架"的（action_log delist/executed）。"""
    return _query("""
        SELECT s.shop_sku, s.store, s.operator, s.supplier, s.orders, s.sale, s.profit_gross,
               s.returns_cnt, s.loss_expected, s.net, s.margin
        FROM order_system.profit_sku_90d s
        WHERE s.returns_cnt >= 2 AND s.net < -50
          AND NOT EXISTS (
            SELECT 1 FROM order_system.action_log a
            WHERE a.action_type='delist' AND a.status='executed'
              AND a.target=CONCAT(s.shop_sku,'@',s.store))
        ORDER BY s.net ASC LIMIT 300""")


def _delist_marked() -> List[Dict]:
    return _query("""
        SELECT id, target, store, created_at
        FROM order_system.action_log
        WHERE action_type='delist' AND status='executed'
        ORDER BY created_at DESC LIMIT 200""")


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
    # 已标记下架的SKU若之后仍有销量 → 顶部警告（由每日规则引擎写入 issue_log）
    delist_warns = _query("""
        SELECT entity, impact_usd, evidence FROM order_system.issue_log
        WHERE issue_type='delisted_but_selling' AND status='open'
        ORDER BY impact_usd DESC""")
    return render_template("profit_control/actions.html",
                           recover=recover[:100], delist=delist[:100],
                           raise_rows=raise_rows[:100], totals=totals,
                           expired=_recover_expired_stats(),
                           expired_rows=_recover_expired_list()[:200],
                           marked=_delist_marked(), delist_warns=delist_warns,
                           counts={"recover": len(recover), "delist": len(delist),
                                   "raise": len(raise_rows)})


# ---------------------------------------------------------------
# 行动交互（写路径：备注 / 标记下架 / 撤销）
# ---------------------------------------------------------------

def _exec(sql: str, params) -> None:
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


@profit_control_bp.route("/recover/note", methods=["POST"])
def recover_note():
    data = request.get_json(silent=True) or {}
    rid = int(data.get("id") or 0)
    note = (data.get("note") or "").strip()[:250]
    if not rid:
        return jsonify({"ok": False, "msg": "missing id"}), 400
    _exec("UPDATE order_system.return_case SET recover_note=%s, note_time=NOW() WHERE id=%s",
          (note or None, rid))
    return jsonify({"ok": True})


@profit_control_bp.route("/delist/mark", methods=["POST"])
def delist_mark():
    data = request.get_json(silent=True) or {}
    sku = (data.get("sku") or "").strip()
    store = (data.get("store") or "").strip()
    if not sku or not store:
        return jsonify({"ok": False, "msg": "missing sku/store"}), 400
    target = f"{sku}@{store}"
    exists = _query("""SELECT id FROM order_system.action_log
                       WHERE action_type='delist' AND status='executed' AND target=%s""",
                    (target,))
    if not exists:
        _exec("""INSERT INTO order_system.action_log (action_type, target, store, status)
                 VALUES ('delist', %s, %s, 'executed')""", (target, store))
    return jsonify({"ok": True})


@profit_control_bp.route("/delist/unmark", methods=["POST"])
def delist_unmark():
    data = request.get_json(silent=True) or {}
    aid = int(data.get("id") or 0)
    if not aid:
        return jsonify({"ok": False, "msg": "missing id"}), 400
    _exec("UPDATE order_system.action_log SET status='cancelled' WHERE id=%s AND action_type='delist'",
          (aid,))
    return jsonify({"ok": True})


@profit_control_bp.route("/actions/export")
def actions_export():
    which = request.args.get("list", "recover")
    if which == "recover":
        rows, name = _recover_list(), "追款清单"
    elif which == "recover_expired":
        rows, name = _recover_expired_list(), "超窗未追回"
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
