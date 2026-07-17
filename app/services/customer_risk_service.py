# -*- coding: utf-8 -*-
"""可疑客户分析（2026-07-17用户拍板）。

从"产生过退货的客户"出发：全店铺订单按 邮箱/电话/街道 三条身份线归一聚合，
算每个身份的订单数/退货数/退货率/退货原因/not_charged次数，按阈值分 high/mid/low，
页面一键写入现有 customer_blacklist（导单自动标红的拦截链路直接生效）。

归一规则（解决同一人大小写/写法不一）：
  邮箱→小写；电话→只留数字取后10位；街道→小写+去标点+缩写统一(Street→st等)+去空格+拼zip前5位。
街道键带zip，避免不同城市同名街道误合并。
"""
import json
import re
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from app.models.db_manager import DBManager

# ---- 判定阈值（草案，按实际分布校准后老板可改）----
HIGH_RULES_DOC = "≥3单且退货率≥50% / 退货≥2且跨店铺 / 同址≥2个名字且退货≥2 / 退货≥3"
MID_RULES_DOC = "退货≥2且率≥50% / 退货≥2且理由集中defective类 / 2单2退"

SUSPECT_REASONS = ("defective", "not as described", "damaged", "quality")

ORDER_TABLES = ("lowes_order_data", "macy_order_data", "bestbuy_order_data")

_ABBR = {"street": "st", "avenue": "ave", "boulevard": "blvd", "drive": "dr",
         "road": "rd", "lane": "ln", "court": "ct", "circle": "cir", "place": "pl",
         "highway": "hwy", "apartment": "apt", "suite": "ste",
         "north": "n", "south": "s", "east": "e", "west": "w"}


def _q(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params) if params else cur.execute(sql)
        return cur.fetchall() or []


def norm_email(e: Optional[str]) -> str:
    e = (e or "").strip().lower()
    return e if "@" in e else ""


def norm_phone(p: Optional[str]) -> str:
    d = re.sub(r"\D", "", p or "")
    if len(d) >= 10:
        return d[-10:]
    return d if len(d) >= 7 else ""


def norm_street(s1: Optional[str], s2: Optional[str], zipc: Optional[str]) -> str:
    s = f"{s1 or ''} {s2 or ''}".lower()
    s = re.sub(r"[^\w\s]", " ", s)
    toks = [_ABBR.get(t, t) for t in s.split()]
    key = "".join(toks)
    if not key or len(key) < 6:
        return ""
    return f"{key}|{(zipc or '').strip()[:5]}"


def _parse_order_customer(raw_json: Optional[str]) -> Dict[str, str]:
    out = {"name": "", "phone": "", "street": "", "street2": ""}
    if not raw_json:
        return out
    try:
        c = (json.loads(raw_json) or {}).get("customer") or {}
    except (TypeError, ValueError):
        return out
    ship = c.get("shipping_address") or {}
    fn = (ship.get("firstname") or c.get("firstname") or "").strip()
    ln = (ship.get("lastname") or c.get("lastname") or "").strip()
    out["name"] = f"{fn} {ln}".strip()
    out["phone"] = ship.get("phone") or ""
    out["street"] = ship.get("street_1") or ""
    out["street2"] = ship.get("street_2") or ""
    return out


def rebuild_profiles() -> Dict[str, Any]:
    """全量重建风险档案（只存有退货的身份）。约几分钟，cron每日跑。"""
    conn = DBManager.get_connection()
    try:
        # 退货订单集 + 原因
        ret_reasons: Dict[str, List[str]] = {}
        for r in _q(conn, """SELECT order_id, reason_code FROM order_system.mirakl_returns"""):
            ret_reasons.setdefault(r["order_id"], []).append((r["reason_code"] or "").strip())
        not_charged: Set[str] = {r["order_id"] for r in _q(conn, """
            SELECT DISTINCT order_id FROM order_system.return_case
            WHERE state='not_charged'""")}

        # 全店订单：每单只取一行（MIN(id)），姓名/电话/街道用JSON函数在数据库侧抽取——
        # 绝不整包回传raw_json（几万单×几十KB曾把查询卡在Sending to client半小时,2026-07-17踩过）
        def _jx(path: str, alias: str) -> str:
            return (f"CASE WHEN d.raw_json IS NOT NULL AND JSON_VALID(d.raw_json) "
                    f"THEN JSON_UNQUOTE(JSON_EXTRACT(d.raw_json, '{path}')) END AS {alias}")

        orders: Dict[str, Dict] = {}
        for tbl in ORDER_TABLES:
            for r in _q(conn, f"""
                    SELECT d.order_id, d.created_date, d.customer_email,
                           d.shipping_city, d.shipping_state, d.shipping_zip,
                           sc.platform, sc.shop_name,
                           {_jx('$.customer.shipping_address.firstname', 'fn')},
                           {_jx('$.customer.shipping_address.lastname', 'ln')},
                           {_jx('$.customer.shipping_address.phone', 'phone')},
                           {_jx('$.customer.shipping_address.street_1', 's1')},
                           {_jx('$.customer.shipping_address.street_2', 's2')}
                    FROM order_system.{tbl} d
                    JOIN (SELECT MIN(id) AS mid FROM order_system.{tbl}
                          WHERE order_state<>'CANCELED' GROUP BY order_id) f ON f.mid=d.id
                    JOIN order_system.shop_configs sc ON sc.id=d.shop_id"""):
                oid = r["order_id"]
                if oid in orders:
                    continue
                cust = {"name": f"{(r['fn'] or '').strip()} {(r['ln'] or '').strip()}".strip(),
                        "phone": r["phone"] or "",
                        "street": r["s1"] or "", "street2": r["s2"] or ""}
                orders[oid] = {
                    "order_id": oid,
                    "store": f"{r['platform']}-{r['shop_name']}",
                    "date": r["created_date"],
                    "email": norm_email(r["customer_email"]),
                    "email_raw": (r["customer_email"] or "").strip(),
                    "phone": norm_phone(cust["phone"]),
                    "phone_raw": cust["phone"],
                    "street_key": norm_street(cust["street"], cust["street2"],
                                              r["shipping_zip"]),
                    "street_raw": (cust["street"] or "").strip(),
                    "name": cust["name"],
                    "city": r["shipping_city"] or "", "state": r["shipping_state"] or "",
                    "zip": r["shipping_zip"] or "",
                    "returned": oid in ret_reasons,
                    "reasons": ret_reasons.get(oid, []),
                    "not_charged": oid in not_charged,
                }
    finally:
        conn.close()

    # 三条身份线聚合
    lines: Dict[tuple, List[Dict]] = {}
    for o in orders.values():
        for id_type, key in (("email", o["email"]), ("phone", o["phone"]),
                             ("street", o["street_key"])):
            if key:
                lines.setdefault((id_type, key), []).append(o)

    rows = []
    now = datetime.now()
    for (id_type, key), os_ in lines.items():
        rets = [o for o in os_ if o["returned"]]
        if not rets:
            continue          # 只关注产生过退货的身份
        n, rn = len(os_), len(rets)
        rate = rn / n
        names = sorted({o["name"] for o in os_ if o["name"]})
        stores = sorted({o["store"] for o in os_})
        reasons = Counter(r for o in rets for r in o["reasons"] if r)
        nc = sum(1 for o in os_ if o["not_charged"])
        suspect_reason_n = sum(c for r, c in reasons.items()
                               if any(s in r.lower() for s in SUSPECT_REASONS))

        risk, why = "low", f"{n}单{rn}退"
        if n >= 3 and rate >= 0.5:
            risk, why = "high", f"{n}单退{rn}（退货率{rate:.0%}）"
        elif rn >= 2 and len(stores) >= 2:
            risk, why = "high", f"跨{len(stores)}个店铺退了{rn}单"
        elif id_type == "street" and len(names) >= 2 and rn >= 2:
            risk, why = "high", f"同一地址{len(names)}个名字下单、退{rn}次（疑似职业退货地址）"
        elif rn >= 3:
            risk, why = "high", f"累计退货{rn}次"
        elif rn >= 2 and rate >= 0.5:
            risk, why = "mid", f"{n}单退{rn}（率{rate:.0%}）"
        elif rn >= 2 and suspect_reason_n >= 2:
            risk, why = "mid", f"退{rn}次且理由集中在defective类（最易白嫖的理由）"

        rep = max(os_, key=lambda o: o["date"] or now)   # 用最近一单做代表信息
        display = (rep["email_raw"] if id_type == "email"
                   else rep["phone_raw"] if id_type == "phone"
                   else rep["street_raw"])
        sample = ",".join(o["order_id"] for o in
                          sorted(os_, key=lambda o: o["date"] or now, reverse=True)[:8])
        reasons_txt = "; ".join(f"{r}×{c}" for r, c in reasons.most_common(3))
        dates = [o["date"] for o in os_ if o["date"]]
        rows.append((id_type, key[:255], (display or "")[:255],
                     (", ".join(names))[:500], (", ".join(stores))[:300],
                     n, rn, round(rate, 4), nc, reasons_txt[:500],
                     min(dates).date() if dates else None,
                     max(dates).date() if dates else None,
                     sample[:700],
                     (rep["name"] or "")[:120], (rep["phone_raw"] or "")[:40],
                     (rep["email_raw"] or "")[:160], (rep["street_raw"] or "")[:255],
                     (rep["city"] or "")[:80], (rep["state"] or "")[:40],
                     (rep["zip"] or "")[:16],
                     risk, why[:300], now))

    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM order_system.customer_risk_profile")
            sql = """INSERT INTO order_system.customer_risk_profile
                (id_type, id_norm, display, names, stores, orders_n, returns_n,
                 return_rate, not_charged_n, reasons, first_order, last_order,
                 sample_orders, rep_name, rep_phone, rep_email, rep_street,
                 rep_city, rep_state, rep_zip, risk_level, risk_reason, built_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE orders_n=VALUES(orders_n)"""
            for i in range(0, len(rows), 300):
                cur.executemany(sql, rows[i:i + 300])
        conn.commit()
        # 标记已在黑名单的（邮箱/电话任一命中active名单；Python侧归一后比对）
        bl_emails: Set[str] = set()
        bl_phones: Set[str] = set()
        for b in _q(conn, """SELECT email, phone FROM order_system.customer_blacklist
                             WHERE active=1"""):
            e = norm_email(b["email"])
            if e:
                bl_emails.add(e)
            ph = norm_phone(b["phone"])
            if ph:
                bl_phones.add(ph)
        if bl_emails or bl_phones:
            hit_ids = []
            for p in _q(conn, """SELECT id, rep_email, rep_phone
                                 FROM order_system.customer_risk_profile"""):
                if (norm_email(p["rep_email"]) in bl_emails
                        or (norm_phone(p["rep_phone"]) and
                            norm_phone(p["rep_phone"]) in bl_phones)):
                    hit_ids.append(p["id"])
            if hit_ids:
                with conn.cursor() as cur:
                    ph_ = ",".join(["%s"] * len(hit_ids))
                    cur.execute(f"UPDATE order_system.customer_risk_profile "
                                f"SET blacklisted=1 WHERE id IN ({ph_})", hit_ids)
                conn.commit()
    finally:
        conn.close()

    lv = Counter(r[20] for r in rows)
    return {"orders_scanned": len(orders), "profiles": len(rows),
            "high": lv.get("high", 0), "mid": lv.get("mid", 0), "low": lv.get("low", 0)}


def add_to_blacklist(profile_id: int, created_by: str = "可疑客户分析") -> Dict[str, Any]:
    conn = DBManager.get_connection()
    try:
        rows = _q(conn, "SELECT * FROM order_system.customer_risk_profile WHERE id=%s",
                  (profile_id,))
        if not rows:
            return {"success": False, "msg": "档案不存在"}
        p = rows[0]
        dup = _q(conn, """SELECT id FROM order_system.customer_blacklist
                          WHERE active=1 AND (
                                (email<>'' AND %s<>'' AND LOWER(email)=LOWER(%s))
                             OR (street<>'' AND %s<>'' AND LOWER(street)=LOWER(%s)))
                          LIMIT 1""",
                  (p["rep_email"], p["rep_email"], p["rep_street"], p["rep_street"]))
        if dup:
            with conn.cursor() as cur:
                cur.execute("UPDATE order_system.customer_risk_profile "
                            "SET blacklisted=1 WHERE id=%s", (profile_id,))
            conn.commit()
            return {"success": True, "msg": "该客户已在黑名单里，已标记"}
        reason = (f"可疑客户分析：{p['risk_reason']}；{p['orders_n']}单{p['returns_n']}退"
                  f"（{p['stores']}）")[:250]
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO order_system.customer_blacklist
                    (full_name, phone, email, street, city, state, zip,
                     reason, active, source, created_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s)""",
                (p["rep_name"], p["rep_phone"], p["rep_email"], p["rep_street"],
                 p["rep_city"], p["rep_state"], p["rep_zip"], reason,
                 "customer_risk", created_by))
            cur.execute("UPDATE order_system.customer_risk_profile SET blacklisted=1 "
                        "WHERE id=%s", (profile_id,))
        conn.commit()
        return {"success": True, "msg": "已加入黑名单（导单时自动标红）"}
    finally:
        conn.close()
