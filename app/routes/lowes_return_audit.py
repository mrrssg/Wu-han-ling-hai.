# -*- coding: utf-8 -*-
"""Lowes-Autool 退货运费稽核页面（/lowes-return-audit）。"""
import json

from flask import Blueprint, jsonify, render_template, request

from app.models.db_manager import DBManager

lowes_return_audit_bp = Blueprint("lowes_return_audit", __name__)


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


@lowes_return_audit_bp.route("/")
def page():
    f_match = (request.args.get("m") or "").strip()   # tracking/po/inferred/none/''
    f_over = request.args.get("over") == "1"           # 只看运费>货值

    # 全量载入(数据量小)，逐行算"运费>货值"：有成本用成本，推断行用候选最低成本
    all_rows = _query("""SELECT * FROM order_system.fedex_return_audit
                         ORDER BY net_charge DESC""")
    over_n = 0
    over_loss = 0.0
    for r in all_rows:
        r["candidates"] = json.loads(r["candidates_json"]) if r.get("candidates_json") else []
        nc = float(r.get("net_charge") or 0)
        cost = r.get("cost")
        r["over"], r["over_amt"], r["over_suspect"] = False, 0.0, False
        if cost is not None and float(cost) > 0:
            if nc > float(cost):
                r["over"], r["over_amt"] = True, round(nc - float(cost), 2)
        elif r["candidates"]:
            costs = [float(c["cost"]) for c in r["candidates"] if c.get("cost") is not None]
            if costs and nc > min(costs):
                r["over"], r["over_amt"], r["over_suspect"] = True, round(nc - min(costs), 2), True
        if r["over"]:
            over_n += 1
            over_loss += r["over_amt"]

    rows = all_rows
    if f_match:
        rows = [r for r in rows if r["match_type"] == f_match]
    if f_over:
        rows = [r for r in rows if r["over"]]

    # 同一订单的多个跟踪号(多箱退货)排到相邻；货值/登记只在该订单首行显示一次(用户要求
    # "每个订单只显示一个货值")。首行取该订单真实货值=组内 max(忽略人工填的0)，其余行 is_dup。
    def _cost_of(x):
        return float(x["cost"]) if x.get("cost") is not None else None
    groups, order_seq = {}, []
    for r in rows:
        gkey = r.get("order_id") or ("__none__" + str(r["tracking"]))  # 未匹配行各自成组
        if gkey not in groups:
            groups[gkey] = []
            order_seq.append(gkey)
        groups[gkey].append(r)
    ordered = []
    for gkey in order_seq:
        g = groups[gkey]
        costs = [c for c in (_cost_of(x) for x in g) if c is not None]
        order_cost = max(costs) if costs else None
        claim = 1 if any(x.get("claim_filed") == 1 for x in g) else \
                (0 if any(x.get("claim_filed") == 0 for x in g) else None)
        # 组内：带全额货值的行排首(✏️预填正确)，再按运费降序
        g.sort(key=lambda x: (-(_cost_of(x) if _cost_of(x) is not None else -1e18),
                              -(float(x.get("net_charge") or 0))))
        for i, x in enumerate(g):
            x["is_dup"] = (i > 0 and len(g) > 1)
            x["order_cost"] = order_cost
            x["order_claim"] = claim
            x["order_span"] = len(g)
        ordered.append((max((float(x.get("net_charge") or 0) for x in g), default=0.0), g))
    ordered.sort(key=lambda t: -t[0])       # 订单组按组内最大退货运费降序(大损失置顶)
    rows = [x for _, g in ordered for x in g]

    # ⚠️ 货值(cost)按【订单】去重，运费(net_charge)/计数按【跟踪号】。
    # 一个订单多箱退货=多个跟踪号行，每行都挂整单货值；直接 SUM(cost) 会把货值加 N 遍。
    # 运费相反：每箱是真实独立的一笔退货运费，多箱=多笔真损失，不能去重。
    stat = _query("""SELECT t.*, o.claim_cost, o.unclaim_cost, o.cost_matched FROM
        (SELECT
            COUNT(*) n, COALESCE(SUM(net_charge),0) ship_total,
            COALESCE(SUM(CASE WHEN order_id IS NOT NULL THEN net_charge END),0) ship_matched,
            SUM(order_id IS NOT NULL) matched_n,
            SUM(match_type='tracking') n_track, SUM(match_type='po') n_po,
            SUM(match_type='inferred') n_infer, SUM(match_type='manual') n_manual,
            SUM(match_type='none') n_none
         FROM order_system.fedex_return_audit) t
        CROSS JOIN
        (SELECT
            COALESCE(SUM(CASE WHEN claim_filed=1 THEN c END),0) claim_cost,
            COALESCE(SUM(CASE WHEN claim_filed=0 THEN c END),0) unclaim_cost,
            COALESCE(SUM(c),0) cost_matched
         FROM (SELECT MAX(cost) c, MAX(claim_filed) claim_filed
               FROM order_system.fedex_return_audit
               WHERE order_id IS NOT NULL GROUP BY order_id) g) o""")
    s = stat[0] if stat else {}
    total = int(s.get("n") or 0)
    matched_n = int(s.get("matched_n") or 0)
    s["match_rate"] = round(matched_n * 100.0 / total, 1) if total else 0.0
    s["over_n"] = over_n
    s["over_loss"] = round(over_loss, 2)
    return render_template("lowes_return_audit/page.html", rows=rows, s=s,
                           total=total, f_match=f_match, f_over=f_over)


@lowes_return_audit_bp.route("/upload", methods=["POST"])
def upload():
    from app.services.lowes_return_audit_service import ingest_and_match
    f = request.files.get("invoice")
    if not f or not f.filename:
        return jsonify({"success": False, "msg": "没选文件"})
    try:
        res = ingest_and_match(f.read(), f.filename)
        return jsonify({"success": True, **res})
    except Exception as exc:
        return jsonify({"success": False, "msg": str(exc)[:300]}), 500


@lowes_return_audit_bp.route("/rematch", methods=["POST"])
def rematch():
    from app.services.lowes_return_audit_service import rematch_all
    try:
        return jsonify({"success": True, **rematch_all(only_unconfirmed=True)})
    except Exception as exc:
        return jsonify({"success": False, "msg": str(exc)[:300]}), 500


@lowes_return_audit_bp.route("/confirm", methods=["POST"])
def confirm():
    from app.services.lowes_return_audit_service import confirm_match
    data = request.get_json(silent=True) or {}
    tracking = (data.get("tracking") or "").strip()
    order_id = (data.get("order_id") or "").strip()
    if not tracking or not order_id:
        return jsonify({"success": False, "msg": "缺参数"})
    try:
        return jsonify(confirm_match(tracking, order_id))
    except Exception as exc:
        return jsonify({"success": False, "msg": str(exc)[:300]}), 500


@lowes_return_audit_bp.route("/manual", methods=["POST"])
def manual():
    from app.services.lowes_return_audit_service import manual_fill
    data = request.get_json(silent=True) or {}
    tracking = (data.get("tracking") or "").strip()
    if not tracking:
        return jsonify({"success": False, "msg": "缺跟踪号"})
    try:
        return jsonify(manual_fill(
            tracking,
            (data.get("order_id") or "").strip() or None,
            (data.get("shop_sku") or "").strip() or None,
            data.get("cost"), data.get("sale")))
    except Exception as exc:
        return jsonify({"success": False, "msg": str(exc)[:300]}), 500


@lowes_return_audit_bp.route("/unbind", methods=["POST"])
def unbind_route():
    from app.services.lowes_return_audit_service import unbind
    data = request.get_json(silent=True) or {}
    tracking = (data.get("tracking") or "").strip()
    if not tracking:
        return jsonify({"success": False, "msg": "缺参数"})
    try:
        return jsonify(unbind(tracking))
    except Exception as exc:
        return jsonify({"success": False, "msg": str(exc)[:300]}), 500
