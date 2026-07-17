# -*- coding: utf-8 -*-
"""产品安全防控 Phase 1（2026-07-17用户拍板）。

链路：录入案例（涉事供应商SKU + PDF/Excel/图片附件）→
  ① 直接命中：供应商SKU → offerprice_listing(4个Mirakl店) + autooperate.mapping_table(其它渠道映射)
     → 各店铺shop SKU，在卖的写 issue_log(safety_risk) 上首页红条
  ② 同款家族：同SKU前缀的变体（GT3021GR+/BL 同模具同风险）自动并入
Phase 2(待做)：AI风险指纹提炼 + 全库举一反三 + 新offer增量夜筛。
"""
import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from app.models.db_manager import DBManager

CASE_TYPES = ["侵权", "Prop65", "危险品", "召回", "其它"]
ALLOWED_EXT = {".pdf", ".xlsx", ".xls", ".png", ".jpg", ".jpeg", ".webp"}

_SKU_TOKEN = re.compile(r"^[A-Za-z0-9+\-]{4,30}$")


def _q(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params) if params else cur.execute(sql)
        return cur.fetchall() or []


def files_root() -> str:
    from flask import current_app
    base = current_app.config.get("BASE_DIR", current_app.root_path)
    d = os.path.join(base, "instance", "safety_cases")
    os.makedirs(d, exist_ok=True)
    return d


def parse_xlsx_skus(path: str) -> Set[str]:
    """从Excel附件里薅出候选SKU token（随后必须过 _validate_supplier_skus）。"""
    from openpyxl import load_workbook
    out: Set[str] = set()
    wb = load_workbook(path, read_only=True, data_only=True)
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            for v in row:
                if v is None:
                    continue
                s = str(v).strip()
                if _SKU_TOKEN.match(s) and any(ch.isdigit() for ch in s):
                    out.add(s)
    wb.close()
    return out


def _validate_supplier_skus(conn, candidates: Set[str]) -> Set[str]:
    """只留真实存在的供应商SKU（供应商表 ∪ offer表warehouse ∪ 映射表warehouse）。"""
    valid: Set[str] = set()
    cands = [c for c in candidates if c]
    for i in range(0, len(cands), 500):
        chunk = cands[i:i + 500]
        ph = ",".join(["%s"] * len(chunk))
        for sql in (
            f"SELECT SKU AS s FROM autooperate.newestdropship WHERE SKU IN ({ph})",
            f"SELECT SKU AS s FROM autooperate.newestdropship_vevor WHERE SKU IN ({ph})",
            f"SELECT DISTINCT warehouse_sku AS s FROM order_system.offerprice_listing "
            f"WHERE warehouse_sku IN ({ph})",
            f"SELECT DISTINCT warehouse_SKU AS s FROM autooperate.mapping_table "
            f"WHERE warehouse_SKU IN ({ph})",
        ):
            for r in _q(conn, sql, chunk):
                if r["s"]:
                    valid.add(r["s"])
    return valid


def _family_base(sku: str) -> str:
    """GT3021GR+ → GT3021（去尾部颜色后缀）。太短的不冒险截。"""
    s = (sku or "").strip().rstrip("+")
    m = re.match(r"^(.*?\d)[A-Za-z]{0,4}$", s)
    base = m.group(1) if m else s
    return base if len(base) >= 5 else s


def expand_family(conn, skus: Set[str]) -> Dict[str, str]:
    """{家族SKU: 触发它的案例SKU}。同前缀且base一致才算同款（防GT3021误吞GT30215）。"""
    fam: Dict[str, str] = {}
    for sku in skus:
        fam.setdefault(sku, sku)
        base = _family_base(sku)
        like = base + "%"
        for sql in (
            "SELECT SKU AS s FROM autooperate.newestdropship WHERE SKU LIKE %s",
            "SELECT SKU AS s FROM autooperate.newestdropship_vevor WHERE SKU LIKE %s",
            "SELECT DISTINCT warehouse_sku AS s FROM order_system.offerprice_listing "
            "WHERE warehouse_sku LIKE %s",
        ):
            for r in _q(conn, sql, (like,)):
                s2 = r["s"]
                if s2 and _family_base(s2) == base:
                    fam.setdefault(s2, sku)
    return fam


def _resolve_hits(conn, fam: Dict[str, str], direct: Set[str]) -> List[Dict]:
    """家族SKU → 各店铺shop SKU（含停卖的），映射表兜非Mirakl渠道。"""
    hits: List[Dict] = []
    seen_shop: Set[str] = set()
    fam_skus = list(fam)
    for i in range(0, len(fam_skus), 500):
        chunk = fam_skus[i:i + 500]
        ph = ",".join(["%s"] * len(chunk))
        for r in _q(conn, f"""
                SELECT platform, shop_name, shop_sku, warehouse_sku, active
                FROM order_system.offerprice_listing
                WHERE warehouse_sku IN ({ph})""", chunk):
            hits.append({
                "supplier_sku": r["warehouse_sku"],
                "source_sku": fam[r["warehouse_sku"]],
                "hit_type": "direct" if r["warehouse_sku"] in direct else "family",
                "platform": r["platform"], "shop_name": r["shop_name"],
                "shop_sku": r["shop_sku"], "active": int(r["active"] or 0),
            })
            seen_shop.add(r["shop_sku"])
        for r in _q(conn, f"""
                SELECT SKU AS shop_sku, warehouse_SKU AS wsku, owner
                FROM autooperate.mapping_table WHERE warehouse_SKU IN ({ph})""", chunk):
            if r["shop_sku"] in seen_shop:
                continue
            hits.append({
                "supplier_sku": r["wsku"], "source_sku": fam.get(r["wsku"], r["wsku"]),
                "hit_type": "direct" if r["wsku"] in direct else "family",
                "platform": "(映射表)", "shop_name": r["owner"] or "?",
                "shop_sku": r["shop_sku"], "active": None,
            })
            seen_shop.add(r["shop_sku"])

    # 近90天单量（在卖优先级排序用；按offer_sku跨店聚合即可）
    if hits:
        shop_skus = list({h["shop_sku"] for h in hits})
        counts: Dict[str, int] = {}
        for i in range(0, len(shop_skus), 300):
            chunk = shop_skus[i:i + 300]
            ph = ",".join(["%s"] * len(chunk))
            for tbl in ("lowes_order_data", "macy_order_data", "bestbuy_order_data"):
                for r in _q(conn, f"""
                        SELECT offer_sku AS s, COUNT(DISTINCT order_id) AS n
                        FROM order_system.{tbl}
                        WHERE offer_sku IN ({ph}) AND order_state<>'CANCELED'
                          AND created_date >= DATE_SUB(CURDATE(), INTERVAL 90 DAY)
                        GROUP BY offer_sku""", chunk):
                    counts[r["s"]] = counts.get(r["s"], 0) + int(r["n"] or 0)
        for h in hits:
            h["orders_90d"] = counts.get(h["shop_sku"], 0)
    return hits


def create_case(case_type: str, title: str, supplier: str, case_text: str,
                skus_text: str, saved_files: List[Dict]) -> Dict[str, Any]:
    """建案例 + 解析Excel附件里的SKU + 命中解析 + 在卖的写issue_log红条。"""
    typed = {s.strip() for s in re.split(r"[\s,，;；\n]+", skus_text or "") if s.strip()}
    excel_skus: Set[str] = set()
    for f in saved_files:
        if f["path"].lower().endswith((".xlsx", ".xls")):
            try:
                excel_skus |= parse_xlsx_skus(f["path"])
            except Exception:
                pass

    conn = DBManager.get_connection()
    try:
        valid = _validate_supplier_skus(conn, typed | excel_skus)
        invalid_typed = sorted(typed - valid)
        direct = valid
        fam = expand_family(conn, direct) if direct else {}
        hits = _resolve_hits(conn, fam, direct) if fam else []

        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO order_system.safety_case
                    (case_type, title, supplier, case_text, supplier_skus, files_json)
                VALUES (%s,%s,%s,%s,%s,%s)""",
                (case_type, title, supplier, case_text,
                 ",".join(sorted(direct)),
                 json.dumps([{"name": f["name"],
                              "path": os.path.basename(os.path.dirname(f["path"])) + "/" + f["name"]}
                             for f in saved_files], ensure_ascii=False)))
            case_id = cur.lastrowid
            for h in hits:
                cur.execute("""
                    INSERT IGNORE INTO order_system.safety_hit
                        (case_id, supplier_sku, source_sku, hit_type, platform,
                         shop_name, shop_sku, active, orders_90d, reason)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (case_id, h["supplier_sku"], h["source_sku"], h["hit_type"],
                     h["platform"], h["shop_name"], h["shop_sku"], h["active"],
                     h.get("orders_90d", 0),
                     f"案例[{title}] {'涉事SKU' if h['hit_type']=='direct' else '同款家族'}："
                     f"{h['supplier_sku']}"))
            # 在卖的 → 首页红条（issue_log 去重插入）
            for h in hits:
                if h["active"] != 1:
                    continue
                entity = f"{h['shop_sku']}@{h['platform']}-{h['shop_name']}"
                cur.execute("""
                    SELECT id FROM order_system.issue_log
                    WHERE issue_type='safety_risk' AND entity=%s AND status='open'""",
                    (entity,))
                if not cur.fetchone():
                    cur.execute("""
                        INSERT INTO order_system.issue_log
                            (detected_date, issue_type, entity, severity, impact_usd,
                             evidence, suggestion, status)
                        VALUES (CURDATE(), 'safety_risk', %s, 'high', 0, %s,
                                '立即评估下架；处理后在 安全防控 页把该命中标为已下架', 'open')""",
                        (entity,
                         f"安全案例#{case_id}[{case_type}]{title}：供应商SKU "
                         f"{h['supplier_sku']}（{'涉事' if h['hit_type']=='direct' else '同款家族'}）在卖"))
        conn.commit()
    finally:
        conn.close()

    selling = sum(1 for h in hits if h["active"] == 1)
    return {"case_id": case_id, "skus_valid": len(direct),
            "skus_invalid": invalid_typed, "family_size": len(fam),
            "hits": len(hits), "selling": selling}


def mark_hit(hit_id: int, status: str) -> bool:
    if status not in ("delisted", "false_positive", "open"):
        return False
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM order_system.safety_hit WHERE id=%s", (hit_id,))
            h = cur.fetchone()
            if not h:
                return False
            cur.execute("UPDATE order_system.safety_hit SET status=%s WHERE id=%s",
                        (status, hit_id))
            if status in ("delisted", "false_positive"):
                entity = f"{h['shop_sku']}@{h['platform']}-{h['shop_name']}"
                cur.execute("""
                    UPDATE order_system.issue_log SET status='resolved'
                    WHERE issue_type='safety_risk' AND entity=%s AND status='open'""",
                    (entity,))
        conn.commit()
        return True
    finally:
        conn.close()


def close_case(case_id: int) -> None:
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE order_system.safety_case SET status='closed' WHERE id=%s",
                        (case_id,))
            cur.execute("""
                UPDATE order_system.issue_log i
                JOIN order_system.safety_hit h
                  ON i.entity = CONCAT(h.shop_sku,'@',h.platform,'-',h.shop_name)
                SET i.status='resolved'
                WHERE h.case_id=%s AND i.issue_type='safety_risk' AND i.status='open'""",
                (case_id,))
        conn.commit()
    finally:
        conn.close()
