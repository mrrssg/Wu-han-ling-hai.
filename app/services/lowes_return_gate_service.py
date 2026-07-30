# -*- coding: utf-8 -*-
"""Lowes-Autool 退货把关。

平台发邮件让我们确认"退不退"。把关=对比【货值成本】vs【退货运费成本】决策:
  货值 > 运费 就退(把货收回来划算)。
难点:很多货平板小包装发出,客户组装了/是伸缩开合件,退回包装尺寸更大。
所以算两个运费:
  ①原始尺寸运费(costway_box_dims 逐箱,warehouse_sku 按"+"拆,单箱=精确码/多箱=码-箱号)——下限;
  ②AI估算"组装/伸缩后退回尺寸"运费(gpt,缓存 lowes_return_ai_dims)——上限。
三档:两运费都<货值→🟢直接给账号;原始<货值但AI估算≥货值→🟡去要真实退回尺寸;原始≥货值→🔴不退;
      组件码在尺寸表查不到→🚩缺尺寸·需人工(不猜)。
目的地固定退货仓 92337,第三方计费(退货billing到我方账户)。
"""
import json
from decimal import Decimal

from app.models.db_manager import DBManager
from app.services import fedex_return_service

STORE_KEY = "lowes_autool"
SHOP_NAME = "autool"

AI_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS order_system.lowes_return_ai_dims (
  shop_sku VARCHAR(80) NOT NULL PRIMARY KEY,
  est_l DECIMAL(7,2), est_w DECIMAL(7,2), est_h DECIMAL(7,2), est_wt DECIMAL(9,3),
  reason VARCHAR(500), updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def _q(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params) if params else cur.execute(sql)
        return cur.fetchall() or []


def parse_components(wh):
    return [c.strip() for c in (wh or "").split("+") if c.strip()]


import re

_BOX_INDEX = None
_SUFFIX_RE = re.compile(r"^(.+)-\d\d$")   # 多箱后缀 -箱号总数(如 -14),字母色号后缀(如 -PI)不算


def _base_of(cpbh):
    m = _SUFFIX_RE.match(cpbh or "")
    return m.group(1) if m else cpbh


def _box_index(conn):
    """{组件基码 -> [箱]}。costway_box_dims 一次性载入进程缓存;多箱按'码-箱号'归到基码。"""
    global _BOX_INDEX
    if _BOX_INDEX is not None:
        return _BOX_INDEX
    idx = {}
    for r in _q(conn, "SELECT cpbh,l_in,w_in,h_in,weight_lb FROM order_system.costway_box_dims"):
        if not (r["l_in"] and r["w_in"] and r["h_in"]):
            continue
        idx.setdefault(_base_of(r["cpbh"]), []).append(
            {"cpbh": r["cpbh"], "L": float(r["l_in"]), "W": float(r["w_in"]),
             "H": float(r["h_in"]), "wt": float(r["weight_lb"] or 0)})
    _BOX_INDEX = idx
    return idx


def _boxes_for_codes(conn, codes):
    """返回 (boxes, missing)。用内存索引:基码命中=该产品所有箱;命中不到=缺尺寸(不猜)。"""
    idx = _box_index(conn)
    boxes, missing = [], []
    for code in codes:
        b = idx.get(code)
        if b:
            boxes.extend(b)
        else:
            missing.append(code)
    return boxes, missing


def _freight(origin_zip, packages):
    """逐箱算FedEx退货运费(第三方计费)加总。packages=[{L,W,H,wt,...}]。
    返回 (Decimal total 或 None, details, err)。"""
    total, details = Decimal("0"), []
    for p in packages:
        res = fedex_return_service.estimate(
            origin_zip, p["L"], p["W"], p["H"], (p.get("wt") or None),
            billing="third-party")
        if not res.get("ok"):
            return None, details, (res.get("msg") or res.get("zone_note") or "算不出")
        t = Decimal(str(res["total"]))
        total += t
        details.append({"cpbh": p.get("cpbh", ""),
                        "dims": f'{p["L"]:.0f}x{p["W"]:.0f}x{p["H"]:.0f}',
                        "freight": f"{t:.2f}"})
    return total, details, None


def _decide(orig, ai, goods_value):
    """三档决策。orig/ai/goods_value 为 Decimal 或 None。"""
    gv = goods_value
    if orig is None:
        return "missing", "缺尺寸·需人工"
    if gv is None or gv <= 0:
        return "no_cost", "缺货值·需人工"
    if orig >= gv:
        return "no_return", "不退(原始运费≥货值)"
    # orig < gv
    if ai is not None and ai < gv:
        return "return", "直接给账号退(两运费都<货值)"
    if ai is not None and ai >= gv:
        return "ask_dims", "去要真实退回尺寸(临界)"
    return "need_ai", "待AI估算退回尺寸"


def get_ai_dims(conn, shop_sku):
    r = _q(conn, """SELECT est_l,est_w,est_h,est_wt,reason FROM order_system.lowes_return_ai_dims
                    WHERE shop_sku=%s""", (shop_sku,))
    if not r or not r[0]["est_l"]:
        return None
    x = r[0]
    return {"L": float(x["est_l"]), "W": float(x["est_w"]), "H": float(x["est_h"]),
            "wt": float(x["est_wt"] or 0), "reason": x["reason"]}


def list_pending(limit=200):
    """待决策退货(IN_PROGRESS)逐笔:SKU/客户ZIP/货值/原始尺寸→运费→三档建议。"""
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(AI_CACHE_DDL)
        conn.commit()
        rows = _q(conn, """
            SELECT r.return_id, r.order_id, r.date_created, r.reason_code,
                   o.offer_sku, o.shipping_zip, o.shipping_state, o.shipping_city,
                   ol.warehouse_sku,
                   pt.cost_price, pt.category
            FROM order_system.mirakl_returns r
            LEFT JOIN order_system.lowes_order_data o ON o.order_id=r.order_id
            LEFT JOIN order_system.offerprice_listing ol
                   ON ol.shop_sku=o.offer_sku AND ol.platform='Lowes' AND ol.shop_name=%s
            LEFT JOIN order_system.pricing_tier pt
                   ON pt.shop_sku=o.offer_sku AND pt.store_key=%s
            WHERE r.shop_name=%s AND r.state='IN_PROGRESS'
            ORDER BY r.date_created DESC LIMIT %s""",
                  (SHOP_NAME, STORE_KEY, SHOP_NAME, limit))
        # AI缓存一次性批量取
        skus = list({r["offer_sku"] for r in rows if r["offer_sku"]})
        ai_map = {}
        if skus:
            ph = ",".join(["%s"] * len(skus))
            for a in _q(conn, f"""SELECT shop_sku,est_l,est_w,est_h,est_wt
                                  FROM order_system.lowes_return_ai_dims
                                  WHERE shop_sku IN ({ph})""", tuple(skus)):
                if a["est_l"]:
                    ai_map[a["shop_sku"]] = {"L": float(a["est_l"]), "W": float(a["est_w"]),
                                             "H": float(a["est_h"]), "wt": float(a["est_wt"] or 0)}
        out = []
        for r in rows:
            sku = r["offer_sku"]
            zipc = (r["shipping_zip"] or "").strip()
            gv = Decimal(str(r["cost_price"])) if r["cost_price"] else None
            item = {
                "return_id": r["return_id"], "order_id": r["order_id"],
                "date": str(r["date_created"])[:10], "reason": r["reason_code"],
                "sku": sku, "zip": zipc, "state": r["shipping_state"], "city": r["shipping_city"],
                "warehouse_sku": r["warehouse_sku"], "category": r["category"],
                "goods_value": (f"{gv:.2f}" if gv else None),
                "orig_freight": None, "ai_freight": None,
                "boxes": [], "missing": [], "err": None,
                "verdict": None, "verdict_text": None,
            }
            if not sku or not zipc:
                item["verdict"], item["verdict_text"] = "missing", "缺SKU或客户ZIP·需人工"
                out.append(item); continue
            codes = parse_components(r["warehouse_sku"])
            boxes, missing = _boxes_for_codes(conn, codes)
            item["missing"] = missing
            orig = None
            if boxes:
                orig, details, err = _freight(zipc, boxes)
                item["boxes"] = details
                item["err"] = err
                if orig is not None:
                    item["orig_freight"] = f"{orig:.2f}"
            if missing and not boxes:
                item["verdict"], item["verdict_text"] = "missing", f"缺尺寸·需人工({','.join(missing)})"
                out.append(item); continue
            # AI 上限(用批量缓存;没缓存留 need_ai)
            ai = ai_map.get(sku)
            ai_f = None
            if ai and orig is not None:
                ai_f, _, aierr = _freight(zipc, [{"cpbh": "AI", "L": ai["L"], "W": ai["W"],
                                                  "H": ai["H"], "wt": ai["wt"]}])
                if ai_f is not None:
                    item["ai_freight"] = f"{ai_f:.2f}"
            v, vt = _decide(orig, ai_f, gv)
            # 有缺件但也有能算的箱:提示
            if missing and v == "return":
                vt += f"(注:{len(missing)}个组件缺尺寸未计入)"
            item["verdict"], item["verdict_text"] = v, vt
            out.append(item)
        return out
    finally:
        conn.close()


def ai_estimate_skus(base_dir, items):
    """对给定SKU调gpt估"组装/伸缩后退回尺寸",缓存 lowes_return_ai_dims。
    items=[{sku, warehouse_sku, category}]。返回新估算的条数。"""
    from app.services.listing_sentinel_service import (
        _openai_client, _ensure_openai_key, MODEL_NAME,
    )
    _ensure_openai_key(base_dir)
    conn = DBManager.get_connection()
    n = 0
    try:
        with conn.cursor() as cur:
            cur.execute(AI_CACHE_DDL)
        conn.commit()
        client = _openai_client()
        seen = set()
        for it in items:
            sku = it["sku"]
            if not sku or sku in seen or get_ai_dims(conn, sku):
                continue
            seen.add(sku)
            boxes, _m = _boxes_for_codes(conn, parse_components(it.get("warehouse_sku")))
            box_txt = "; ".join(f'{b["L"]:.0f}x{b["W"]:.0f}x{b["H"]:.0f}in {b["wt"]:.0f}lb'
                                for b in boxes) or "未知"
            prompt = (
                f"产品SKU {sku},类目 {it.get('category') or '未知'}。"
                f"原始发货为平板包装,共{len(boxes)}箱:{box_txt}。"
                "客户退货时产品可能已组装、或是可伸缩/开合件(如遮阳伞、可延展餐桌、组装家具),"
                "退回包装通常比发货更大。请估算【最坏情况下退回时的单个包装尺寸】(英寸)和重量(磅)。"
                "只输出JSON:{\"L\":数字,\"W\":数字,\"H\":数字,\"weight_lb\":数字,\"reason\":\"一句中文理由\"}。"
                "尺寸用整数英寸,合理保守偏大但不夸张。"
            )
            try:
                resp = client.chat.completions.create(
                    model=MODEL_NAME, response_format={"type": "json_object"},
                    messages=[{"role": "user", "content": prompt}])
                d = json.loads(resp.choices[0].message.content)
                L, W, H = float(d["L"]), float(d["W"]), float(d["H"])
                wt = float(d.get("weight_lb") or sum(b["wt"] for b in boxes) or 0)
                reason = str(d.get("reason") or "")[:480]
            except Exception:
                continue
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO order_system.lowes_return_ai_dims
                    (shop_sku,est_l,est_w,est_h,est_wt,reason) VALUES (%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE est_l=VALUES(est_l),est_w=VALUES(est_w),
                    est_h=VALUES(est_h),est_wt=VALUES(est_wt),reason=VALUES(reason)""",
                            (sku, L, W, H, wt, reason))
            conn.commit()
            n += 1
        return n
    finally:
        conn.close()


def recompute(shop_sku, origin_zip, L, W, H, wt):
    """人工填了真实退回尺寸后重算运费+决策(单个包装)。"""
    conn = DBManager.get_connection()
    try:
        r = _q(conn, """SELECT cost_price FROM order_system.pricing_tier
                        WHERE shop_sku=%s AND store_key=%s""", (shop_sku, STORE_KEY))
        gv = Decimal(str(r[0]["cost_price"])) if r and r[0]["cost_price"] else None
        f, details, err = _freight(origin_zip, [{"cpbh": "手填", "L": float(L), "W": float(W),
                                                 "H": float(H), "wt": float(wt or 0)}])
        if f is None:
            return {"ok": False, "msg": err or "算不出"}
        verdict = "return" if (gv and f < gv) else "no_return"
        return {"ok": True, "freight": f"{f:.2f}",
                "goods_value": (f"{gv:.2f}" if gv else None), "verdict": verdict,
                "verdict_text": ("退(真实运费<货值)" if verdict == "return"
                                 else "不退(真实运费≥货值)")}
    finally:
        conn.close()
