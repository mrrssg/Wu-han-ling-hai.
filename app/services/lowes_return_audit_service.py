# -*- coding: utf-8 -*-
"""Lowes-Autool 退货运费稽核（2026-07-27）。

上传 FedEx 发票 CSV → 每票退货匹配 Lowes 订单：
  ① 跟踪号 → return_case.claim_tracking（精确，直接带订单+货值+已登记）
  ② PO号   → lowesorder 订单号（精确）
  ③ 推断   → 寄件人姓名+邮编 / 城市+邮编 / 门店辐射区+日期 → Top候选
交叉引用飞书退货登记表(return_case.claim_filed)看"豪雅已登记退货货值"。
"""
import csv
import io
import json
from typing import Any, Dict, List, Optional

from app.models.db_manager import DBManager

STORE = "Lowes-Autool"
_RC = "utf8mb4_unicode_ci"   # 跨表 JOIN 统一 collation


# ============================ 解析 FedEx CSV ============================

def _num(s) -> float:
    s = (s or "").replace(",", "").strip()
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def parse_fedex_csv(content: bytes) -> List[Dict[str, Any]]:
    """解析 FedEx 发票 → 只留退货行(收件方=Autool海外仓 Fontana/Ontario)。按跟踪号去重。"""
    text = content.decode("utf-8-sig", errors="replace")
    rows: List[Dict[str, Any]] = []
    seen = set()
    for r in csv.DictReader(io.StringIO(text)):
        rc = (r.get("Recipient Company") or "").strip().upper()
        rn = (r.get("Recipient Name") or "").strip().upper()
        rcity = (r.get("Recipient City") or "").strip().upper()
        if not ("AUTOOL" in rc or "AUTOOL" in rn or rcity in ("FONTANA", "ONTARIO")):
            continue
        trk = (r.get("Express or Ground Tracking ID") or "").strip()
        if not trk:
            continue
        net = _num(r.get("Net Charge Amount")) or _num(r.get("Transportation Charge Amount"))
        key = (trk, round(net, 2))
        if key in seen:
            continue
        seen.add(key)
        sd = (r.get("Shipment Date") or "").strip()
        rows.append({
            "tracking": trk[:40],
            "ship_date": f"{sd[:4]}-{sd[4:6]}-{sd[6:8]}" if len(sd) == 8 else None,
            "net_charge": round(net, 2),
            "po": "".join(c for c in (r.get("Original Ref#3/PO Number") or "") if c.isdigit())[:32],
            "cust_ref": (r.get("Original Customer Reference") or "").strip()[:120],
            "shipper_name": (r.get("Shipper Name") or "").strip()[:120],
            "shipper_city": (r.get("Shipper City") or "").strip()[:80],
            "shipper_state": (r.get("Shipper State") or "").strip()[:16],
            "shipper_zip": (r.get("Shipper Zip Code") or "").strip()[:16],
            "actual_weight": _num(r.get("Actual Weight Amount")),
            "dim": f"{r.get('Dim Length','')}x{r.get('Dim Width','')}x{r.get('Dim Height','')}"[:40],
        })
    return rows


# ============================ 成本/已登记查询 ============================

def _cost_from_costway(cur, costway_sku: Optional[str]) -> Optional[float]:
    """Costway SKU → 成本 = newestdropship.Price × 0.75。带后缀取不到时退回基础SKU。"""
    if not costway_sku:
        return None
    for sku in (costway_sku, costway_sku.rsplit("-", 1)[0]):
        cur.execute("SELECT Price FROM autooperate.newestdropship WHERE SKU=%s LIMIT 1", (sku,))
        r = cur.fetchone()
        if r and r.get("Price") is not None:
            return round(float(r["Price"]) * 0.75, 2)
    return None


def _order_cost_claim(cur, order_id: str, costway_sku: Optional[str] = None) -> Dict[str, Any]:
    """一个订单的 货值/售价/运营/已登记。优先 return_case(权威,含claim_filed)，否则算成本。"""
    cur.execute(f"""SELECT shop_sku, cost, sale, operator, claim_filed
                    FROM order_system.return_case
                    WHERE store=%s AND order_id=%s COLLATE {_RC}
                    ORDER BY cost DESC LIMIT 1""", (STORE, order_id))
    r = cur.fetchone()
    if r:
        return {"shop_sku": r["shop_sku"], "cost": float(r["cost"] or 0),
                "sale": float(r["sale"] or 0),
                "operator": r["operator"], "claim_filed": r["claim_filed"]}
    return {"shop_sku": None, "cost": _cost_from_costway(cur, costway_sku),
            "sale": None, "operator": None, "claim_filed": None}


# ============================ 三层匹配 ============================

def _match_by_tracking(cur, tracking: str) -> Optional[Dict[str, Any]]:
    cur.execute(f"""SELECT order_id, shop_sku, cost, sale, operator, claim_filed
                    FROM order_system.return_case
                    WHERE store=%s AND claim_tracking LIKE %s LIMIT 1""",
                (STORE, f"%{tracking}%"))
    r = cur.fetchone()
    if not r:
        return None
    return {"match_type": "tracking", "order_id": r["order_id"], "shop_sku": r["shop_sku"],
            "cost": float(r["cost"] or 0), "sale": float(r["sale"] or 0),
            "operator": r["operator"], "claim_filed": r["claim_filed"], "candidates_json": None}


def _match_by_po(cur, po: str) -> Optional[Dict[str, Any]]:
    cur.execute("""SELECT `Order number` oid, `Offer SKU` sku, Costway_SKU csku
                   FROM autooperate.lowesorder
                   WHERE `Order number` REGEXP %s ORDER BY `Order number` LIMIT 1""",
                (f"^{po}-",))
    o = cur.fetchone()
    if not o:
        return None
    cc = _order_cost_claim(cur, o["oid"], o["csku"])
    return {"match_type": "po", "order_id": o["oid"], "shop_sku": cc["shop_sku"] or o["sku"],
            "cost": cc["cost"], "sale": cc["sale"], "operator": cc["operator"],
            "claim_filed": cc["claim_filed"], "candidates_json": None}


def _infer_candidates(cur, row: Dict[str, Any]) -> List[Dict[str, Any]]:
    """无PO无跟踪号 → 给候选订单。姓名+邮编 / 城市+邮编 / 门店辐射区+日期。"""
    zip5 = (row.get("shipper_zip") or "")[:5]
    name = (row.get("shipper_name") or "").strip()
    last = name.split()[-1] if name else ""
    is_store = "LOWES" in name.upper() or "STORE" in name.upper()
    cands: List[Dict[str, Any]] = []
    seen = set()

    def add(oid, sku, csku, reason, score):
        if not oid or oid in seen:
            return
        seen.add(oid)
        cc = _order_cost_claim(cur, oid, csku)
        cands.append({"order_id": oid, "shop_sku": cc["shop_sku"] or sku,
                      "cost": cc["cost"], "sale": cc["sale"], "operator": cc["operator"],
                      "claim_filed": cc["claim_filed"], "reason": reason, "score": score})

    # ① 客户直退：姓名(姓)+邮编 → lowesorder
    if not is_store and last and zip5:
        cur.execute("""SELECT `Order number` oid, `Offer SKU` sku, Costway_SKU csku
                       FROM autooperate.lowesorder
                       WHERE UPPER(`Shipping address last name`)=%s
                         AND `Shipping address zip` LIKE %s LIMIT 5""",
                    (last.upper(), f"{zip5}%"))
        for o in cur.fetchall():
            add(o["oid"], o["sku"], o["csku"], "姓名+邮编精配", 95)

    # ② 城市+邮编 → lowes_order_data(同步表无姓名，住宅一般唯一)
    if not is_store and not cands and zip5 and row.get("shipper_city"):
        cur.execute(f"""SELECT order_id oid, offer_sku sku FROM order_system.lowes_order_data
                        WHERE shipping_zip LIKE %s AND UPPER(shipping_city)=%s LIMIT 5""",
                    (f"{zip5}%", row["shipper_city"].upper()))
        for o in cur.fetchall():
            add(o["oid"], o["sku"], None, "城市+邮编", 80)

    # ③ 门店代退 或 前两步无果 → 辐射区(同邮编前2位=同州区)未跟踪待定退货，按退货日期贴近寄出日期
    if not cands and zip5:
        z2 = zip5[:2]
        cur.execute(f"""SELECT rc.order_id oid, rc.shop_sku sku, rc.cost, rc.return_date,
                               lod.shipping_city city, lod.shipping_zip zip
                        FROM order_system.return_case rc
                        JOIN order_system.lowes_order_data lod
                          ON lod.order_id=rc.order_id COLLATE {_RC}
                        WHERE rc.store=%s
                          AND (rc.claim_tracking IS NULL OR TRIM(rc.claim_tracking)='')
                          AND LEFT(REPLACE(lod.shipping_zip,' ',''),2)=%s
                        ORDER BY ABS(DATEDIFF(rc.return_date, %s)) ASC LIMIT 8""",
                    (STORE, z2, row.get("ship_date")))
        for o in cur.fetchall():
            same = (o["zip"] or "").startswith(zip5)
            add(o["oid"], o["sku"], None,
                f"门店辐射区({o['city']} {o['zip']})+退货日期贴近", 55 + (15 if same else 0))

    cands.sort(key=lambda c: -c["score"])
    return cands[:5]


def _match_row(cur, row: Dict[str, Any]) -> Dict[str, Any]:
    m = _match_by_tracking(cur, row["tracking"])
    if m:
        return m
    if row.get("po"):
        m = _match_by_po(cur, row["po"])
        if m:
            return m
    cands = _infer_candidates(cur, row)
    return {"match_type": "inferred" if cands else "none",
            "order_id": None, "shop_sku": None, "cost": None, "sale": None,
            "operator": None, "claim_filed": None,
            "candidates_json": json.dumps(cands, ensure_ascii=False) if cands else None}


# ============================ 入库 + 重匹配 ============================

_COLS = ("tracking,ship_date,net_charge,po,cust_ref,shipper_name,shipper_city,"
         "shipper_state,shipper_zip,actual_weight,dim,invoice_file")


def ingest_and_match(content: bytes, filename: str) -> Dict[str, Any]:
    rows = parse_fedex_csv(content)
    if not rows:
        return {"parsed": 0, "msg": "没解析到退货行(收件方需为Autool海外仓)"}
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            new = 0
            for row in rows:
                cur.execute(f"""INSERT INTO order_system.fedex_return_audit ({_COLS})
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE net_charge=VALUES(net_charge),
                        po=VALUES(po), shipper_name=VALUES(shipper_name),
                        shipper_zip=VALUES(shipper_zip), ship_date=VALUES(ship_date)""",
                    (row["tracking"], row["ship_date"], row["net_charge"], row["po"] or None,
                     row["cust_ref"], row["shipper_name"], row["shipper_city"],
                     row["shipper_state"], row["shipper_zip"], row["actual_weight"] or None,
                     row["dim"], filename[:160]))
                new += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    matched = rematch_all(only_unconfirmed=True)
    return {"parsed": len(rows), "new_or_updated": new, **matched}


def rematch_all(only_unconfirmed: bool = True) -> Dict[str, Any]:
    """对库里(未人工确认的)退货重跑匹配。"""
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            where = "WHERE store_ok" if False else "WHERE 1=1"
            if only_unconfirmed:
                where += " AND confirmed=0"
            cur.execute(f"SELECT * FROM order_system.fedex_return_audit {where}")
            rows = cur.fetchall()
            for row in rows:
                m = _match_row(cur, row)
                cur.execute("""UPDATE order_system.fedex_return_audit
                    SET match_type=%s, order_id=%s, shop_sku=%s, cost=%s, sale=%s,
                        operator=%s, claim_filed=%s, candidates_json=%s, matched_at=NOW()
                    WHERE tracking=%s""",
                    (m["match_type"], m["order_id"], m["shop_sku"], m["cost"], m["sale"],
                     m["operator"], m["claim_filed"], m["candidates_json"], row["tracking"]))
        conn.commit()
    finally:
        conn.close()
    return {"rematched": len(rows)}


def confirm_match(tracking: str, order_id: str) -> Dict[str, Any]:
    """用户从候选里点定一个订单 → 落为 manual 确认。"""
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT candidates_json FROM order_system.fedex_return_audit WHERE tracking=%s",
                        (tracking,))
            r = cur.fetchone()
            pick = None
            if r and r.get("candidates_json"):
                for c in json.loads(r["candidates_json"]):
                    if c["order_id"] == order_id:
                        pick = c
                        break
            if pick is None:
                cc = _order_cost_claim(cur, order_id)
                pick = {"order_id": order_id, "shop_sku": cc["shop_sku"], "cost": cc["cost"],
                        "sale": cc["sale"], "operator": cc["operator"], "claim_filed": cc["claim_filed"]}
            cur.execute("""UPDATE order_system.fedex_return_audit
                SET match_type='manual', order_id=%s, shop_sku=%s, cost=%s, sale=%s,
                    operator=%s, claim_filed=%s, confirmed=1, matched_at=NOW()
                WHERE tracking=%s""",
                (pick["order_id"], pick.get("shop_sku"), pick.get("cost"), pick.get("sale"),
                 pick.get("operator"), pick.get("claim_filed"), tracking))
        conn.commit()
    finally:
        conn.close()
    return {"success": True, "tracking": tracking, "order_id": order_id}


def manual_fill(tracking: str, order_id: Optional[str] = None,
                shop_sku: Optional[str] = None, cost: Any = None,
                sale: Any = None) -> Dict[str, Any]:
    """人工直接填 订单号/SKU/货值(候选都不对、或成本查不到时用)。
    落 match_type='manual' + confirmed=1，rematch 不会覆盖；unbind 可撤销回自动。"""
    def _f(v):
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""UPDATE order_system.fedex_return_audit
                SET match_type='manual', order_id=%s, shop_sku=%s, cost=%s, sale=%s,
                    confirmed=1, matched_at=NOW()
                WHERE tracking=%s""",
                ((order_id or None), (shop_sku or None), _f(cost), _f(sale), tracking))
            n = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return {"success": n > 0, "tracking": tracking}


def unbind(tracking: str) -> Dict[str, Any]:
    """撤销人工确认，回到自动匹配。"""
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE order_system.fedex_return_audit SET confirmed=0 WHERE tracking=%s",
                        (tracking,))
        conn.commit()
    finally:
        conn.close()
    rematch_all(only_unconfirmed=True)
    return {"success": True}
