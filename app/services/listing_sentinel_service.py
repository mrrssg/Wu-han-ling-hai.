# -*- coding: utf-8 -*-
"""
Listing哨兵：每笔退货触发"我方listing vs 供应商listing vs 价格"三方对比。

流程（scripts/listing_sentinel_daily.py 调 run_sentinel(days, limit)）：
  1. 近N天新退货(mirakl_returns×return_case) → 按 SKU×店铺 去重
  2. 每个SKU拼三方数据包：
     我方: 飞书店铺Mirakl表(重写后标题/fnb1-5/长描述/图1-11) + offerprice_listing在线价
     供应商: 飞书Costway/Vevor sku资料(标题/Spec/Description/图集) + newestdropship*现价
     背景: 该SKU近90天退货reason_code分布
  3. GPT(gpt-5.1, 带双方主图视觉对比)输出结构化结论:
     夸大其词 / 属性不符 / 图片问题 / verdict(clean|minor|severe)
  4. 落表 listing_sentinel_findings(UPSERT per SKU×店铺)；severe → issue_log(listing_mismatch)

口径注意:
  - 我方文案以飞书产品表(我们上传的原稿)为准, 不调Mirakl API(用户规则:调API必须走代理;
    v1 无需API, 若后续加 OF21 fallback 必须复用 _load_network_profile)
  - 价格: 我方取 discount_price(无则origin_price); 供应商取 newestdropship 现价(飞书价兜底)
"""
import json
import os
import time
from datetime import date
from typing import Any, Dict, List, Optional

import requests

from app.models.db_manager import DBManager

FEISHU_APP_ID = "cli_a940a2a1067adbd2"
FEISHU_APP_SECRET = "i2mKLGVzUDmu4v0U9HYEYdMGc0ZvZAgU"
PRODUCT_APP = "QEeubiXYGa83zXs3Zt8cSSJPnih"

STORE_TABLES = {
    "Macys-Kuyotq": "tblfyStm2eu3hp1Q",
    "Macys-Wopet": "tbla2i1OwdwlCweK",
    "Lowes-Autool": "tblGp3uvtOe99vjY",
    "Lowes-Yasonic": "tbldeuRJOoJBfX2g",
}
COSTWAY_TBL = "tblDohq1QfgABVeH"
VEVOR_TBL = "tbl4OqqXTIsywAyK"

MODEL_NAME = "gpt-5.1"
MAX_TEXT = 2800   # 喂给模型的单段文本上限


# ------------------------- 基础工具 -------------------------

def _token() -> str:
    r = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}, timeout=30)
    return r.json()["tenant_access_token"]


def _gt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return v.get("text") or v.get("link") or ""
    if isinstance(v, list) and v and isinstance(v[0], dict):
        return "".join(x.get("text") or x.get("name") or "" for x in v)
    return str(v)


def _glink(v) -> str:
    if isinstance(v, dict):
        return v.get("link") or v.get("text") or ""
    return _gt(v)


def _gn(v) -> Optional[float]:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.replace("$", "").replace(",", ""))
        except ValueError:
            return None
    return None


def _feishu_find(headers, table_id: str, field: str, value: str) -> Optional[Dict]:
    url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{PRODUCT_APP}"
           f"/tables/{table_id}/records/search?page_size=3")
    body = {"filter": {"conjunction": "and", "conditions": [
        {"field_name": field, "operator": "is", "value": [value]}]},
        "automatic_fields": False}
    r = requests.post(url, headers=headers, data=json.dumps(body).encode("utf-8"),
                      timeout=30).json()
    items = (r.get("data") or {}).get("items") or []
    return items[0]["fields"] if items else None


def _mysql_one(conn, sql: str, params) -> Optional[Dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


# ------------------------- 数据装配 -------------------------

def _our_listing(headers, conn, store: str, shop_sku: str) -> Optional[Dict]:
    tbl = STORE_TABLES.get(store)
    if tbl:
        f = _feishu_find(headers, tbl, "Shop SKU", shop_sku)
        if f and (_gt(f.get("重写后标题")) or _gt(f.get("productLongDescription"))
                  or _gt(f.get("Item Name"))):
            imgs = [_glink(f.get(f"第{i}张")) for i in range(1, 12)]
            return {
                "title": _gt(f.get("重写后标题")) or _gt(f.get("Item Name")),
                "bullets": [_gt(f.get(f"fnb{i}")) for i in range(1, 6)],
                "long_desc": _gt(f.get("productLongDescription")),
                "images": [u for u in imgs if u and u.startswith("http")],
                "supplier_sku": _gt(f.get("供应商SKU")),
                "supplier": _gt(f.get("供应商")),
                # 真实成交价=折扣后价格(飞书公式)；活动折扣用于兜底换算
                "price_discounted": _gn(f.get("折扣后价格")),
                "discount_factor": _gn(f.get("活动折扣")),
            }
    # 兜底：老SKU(如MRMC前缀)不在飞书新表 → MySQL macy_kuyotq_listing(平台侧快照)
    if store == "Macys-Kuyotq":
        row = _mysql_one(conn, """
            SELECT productname, fnb1, fnb2, fnb3, fnb4, fnb5, productlongdescription,
                   mainimage FROM order_system.macy_kuyotq_listing
            WHERE shopsku=%s LIMIT 1""", (shop_sku,))
        if row and (row["productname"] or row["productlongdescription"]):
            mp = _mysql_one(conn, "SELECT warehouse_SKU FROM autooperate.mapping_table "
                                  "WHERE SKU=%s LIMIT 1", (shop_sku,))
            return {
                "title": row["productname"] or "",
                "bullets": [row[f"fnb{i}"] or "" for i in range(1, 6)],
                "long_desc": row["productlongdescription"] or "",
                "images": [row["mainimage"]] if (row["mainimage"] or "").startswith("http") else [],
                "supplier_sku": (mp or {}).get("warehouse_SKU") or "",
                "supplier": "",
            }
    return None


def _supplier_listing(headers, supplier: str, supplier_sku: str) -> Optional[Dict]:
    s = supplier.strip().lower()
    if s == "costway":
        f = _feishu_find(headers, COSTWAY_TBL, "SKU", supplier_sku)
        if not f:
            return None
        imgs = [_glink(f.get(f"Images{i}")) for i in range(1, 9)]
        return {"title": _gt(f.get("Item Name")),
                "spec": _gt(f.get("Specification")),
                "desc": _gt(f.get("Description")),
                "price": _gn(f.get("Price")),
                "link": _glink(f.get("Item Link")),
                "images": [u for u in imgs if u and u.startswith("http")]}
    if s.startswith("vevor") or s == "司顺":
        f = _feishu_find(headers, VEVOR_TBL, "SKU", supplier_sku)
        if not f:
            return None
        sells = " / ".join(_gt(f.get(f"Selling point {i}")) for i in range(1, 6) if _gt(f.get(f"Selling point {i}")))
        imgs = [_glink(f.get("Image link")), _glink(f.get("goods_main_original_picture"))]
        return {"title": _gt(f.get("Product title")),
                "spec": sells,
                "desc": _gt(f.get("Product description")),
                "price": _gn(f.get("Price")),
                "link": _glink(f.get("Product link")),
                "images": [u for u in imgs if u and u.startswith("http")]}
    return None


STORE_KEYS = {"Macys-Kuyotq": "macy_kuyotq", "Macys-Wopet": "macy_wopet",
              "Lowes-Autool": "lowes_autool", "Lowes-Yasonic": "lowes_yasonic"}
# 店铺级活动折扣兜底（老SKU飞书查不到折扣时用；Kuyotq=0.4 是店铺常量）
STORE_DEFAULT_DISCOUNT = {"Macys-Kuyotq": 0.4}


def _prices(conn, shop_sku: str, store: str, supplier: str, supplier_sku: str,
            feishu_supplier_price, our_listing: Dict) -> Dict:
    """我方真实成交价 = 折扣后价格。取价优先级：
    ①平台在线discount_price ②飞书「折扣后价格」 ③原价×飞书活动折扣
    ④原价×offer_pricing_config的SKU级折扣 ⑤原价×店铺默认折扣 ⑥原价"""
    row = _mysql_one(conn, """
        SELECT origin_price, discount_price FROM order_system.offerprice_listing
        WHERE shop_sku=%s LIMIT 1""", (shop_sku,))
    origin = float(row["origin_price"] or 0) if row else 0.0
    disc_live = float(row["discount_price"] or 0) if row else 0.0
    price_ours = None
    if disc_live > 0:
        price_ours = disc_live
    elif our_listing.get("price_discounted"):
        price_ours = our_listing["price_discounted"]
    elif origin > 0 and our_listing.get("discount_factor"):
        price_ours = round(origin * our_listing["discount_factor"], 2)
    elif origin > 0:
        cfg = _mysql_one(conn, """
            SELECT discount_factor FROM order_system.offer_pricing_config
            WHERE warehouse_sku=%s AND store_key=%s""",
            (supplier_sku, STORE_KEYS.get(store, "")))
        factor = float(cfg["discount_factor"]) if cfg and cfg.get("discount_factor") else \
            STORE_DEFAULT_DISCOUNT.get(store)
        price_ours = round(origin * factor, 2) if factor else origin
    tbl = "newestdropship" if supplier.strip().lower() == "costway" else "newestdropship_vevor"
    sup = _mysql_one(conn, f"SELECT Price FROM autooperate.{tbl} WHERE SKU=%s", (supplier_sku,))
    price_sup = float(sup["Price"]) if sup and sup.get("Price") is not None else feishu_supplier_price
    return {"ours": price_ours, "supplier": price_sup,
            "ratio": round(price_ours / price_sup, 2) if price_ours and price_sup else None}


# ------------------------- AI 对比 -------------------------

def _ensure_openai_key(base_dir: str) -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        key_path = os.path.join(base_dir, "instance", "openai_key.txt")
        if os.path.exists(key_path):
            with open(key_path, "r", encoding="utf-8") as fh:
                key = fh.read().strip()
            if key:
                os.environ["OPENAI_API_KEY"] = key
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set")


def _cut(s: str, n: int = MAX_TEXT) -> str:
    s = (s or "").strip()
    return s[:n]


# OpenAI 从香港服务器直连被 403(unsupported region)，必须走 Brightdata 美国代理
# （不带 -ip- 固定，落在 isp_proxy5 池的任意美国IP上，不占用店铺专用IP的会话）
_OPENAI_PROXY = ("http://brd-customer-hl_14404d60-zone-isp_proxy5:"
                 "f73vfek34d52@brd.superproxy.io:33335")


def _openai_client():
    import httpx
    from openai import OpenAI
    try:
        http_client = httpx.Client(proxy=_OPENAI_PROXY, timeout=180)
    except TypeError:   # 老版本 httpx 用 proxies=
        http_client = httpx.Client(proxies=_OPENAI_PROXY, timeout=180)
    return OpenAI(http_client=http_client)


def _ai_compare(ours: Dict, sup: Dict, prices: Dict, reasons: str) -> Dict:
    client = _openai_client()
    prompt = f"""你是电商listing审计员。买家退货了，请对比"我方listing"与"供应商官方资料"，找出我方文案可能导致退货的问题。

【退货背景】该SKU近90天退货原因分布: {reasons or '无记录'}
【价格】我方售价 ${prices.get('ours')} vs 供应商官网价 ${prices.get('supplier')}（倍数 {prices.get('ratio')}）

【我方listing】
标题: {_cut(ours['title'], 300)}
五点描述: {_cut(' | '.join(b for b in ours['bullets'] if b))}
长描述: {_cut(ours['long_desc'])}

【供应商官方资料】
标题: {_cut(sup['title'], 300)}
规格Spec: {_cut(sup['spec'])}
描述: {_cut(sup['desc'])}

要求：
1. 逐项核对数字型属性(尺寸/容量/承重/功率/件数/材质)，我方比供应商资料夸大或不符的都要列出
2. 我方无中生有的功能承诺(防水/静音/免安装/适用人数等供应商没写的)列为"夸大"
3. 单位换算错误(cm/inch)是常见问题，重点检查
4. 如提供了两张图，判断是否同一产品(image_same_product)
5. 供应商资料里没写≠我方错，只有明确矛盾或明显无依据的承诺才算问题；拿不准的标low
只输出JSON：
{{"verdict":"clean|minor|severe","summary":"一句话中文结论",
"issues":[{{"type":"夸大|属性不符|单位错误|图片|价格风险","severity":"high|mid|low",
"ours":"我方原文摘录","supplier":"供应商依据摘录(没有则写'供应商未提及')","note":"中文说明"}}],
"image_same_product":true/false/null,"image_note":"图片对比说明"}}
severe=存在high问题且与退货原因吻合；minor=只有mid/low问题；clean=没发现问题。"""

    content = [{"type": "text", "text": prompt}]
    if ours.get("images"):
        content.append({"type": "image_url", "image_url": {"url": ours["images"][0]}})
    if sup.get("images"):
        content.append({"type": "image_url", "image_url": {"url": sup["images"][0]}})
    resp = client.chat.completions.create(
        model=MODEL_NAME, response_format={"type": "json_object"},
        messages=[{"role": "system", "content": "Output valid JSON only."},
                  {"role": "user", "content": content}])
    return json.loads(resp.choices[0].message.content)


# ------------------------- 主流程 -------------------------

def run_sentinel(base_dir: str, days: int = 1, limit: int = 0,
                 skus: Optional[List[str]] = None) -> Dict[str, Any]:
    _ensure_openai_key(base_dir)
    headers = {"Authorization": f"Bearer {_token()}",
               "Content-Type": "application/json; charset=utf-8"}
    conn = DBManager.get_connection()
    started = time.time()
    stats = {"targets": 0, "analyzed": 0, "severe": 0, "minor": 0,
             "clean": 0, "skipped": [], "errors": []}
    try:
        with conn.cursor() as cur:
            if skus:
                ph = ",".join(["%s"] * len(skus))
                cur.execute(f"""
                    SELECT rc.shop_sku, rc.store, rc.operator, rc.supplier, COUNT(*) AS n
                    FROM order_system.return_case rc
                    WHERE rc.shop_sku IN ({ph}) AND rc.state <> 'not_charged'
                    GROUP BY rc.shop_sku, rc.store, rc.operator, rc.supplier""", tuple(skus))
            else:
                cur.execute("""
                    SELECT rc.shop_sku, rc.store, rc.operator, rc.supplier, COUNT(*) AS n
                    FROM order_system.return_case rc
                    JOIN order_system.mirakl_returns mr ON mr.order_id = rc.order_id
                    WHERE mr.date_created >= DATE_SUB(NOW(), INTERVAL %s DAY)
                      AND rc.shop_sku <> '' AND rc.state <> 'not_charged'
                    GROUP BY rc.shop_sku, rc.store, rc.operator, rc.supplier
                    ORDER BY n DESC""", (days,))
            targets = cur.fetchall()
        if limit:
            targets = targets[:limit]
        stats["targets"] = len(targets)

        for t in targets:
            sku, store = t["shop_sku"], t["store"]
            try:
                # 近90天 reason 分布
                r = _mysql_one(conn, """
                    SELECT GROUP_CONCAT(CONCAT(x.reason_code,'x',x.cnt) SEPARATOR ', ') AS d FROM (
                      SELECT mr.reason_code, COUNT(*) cnt
                      FROM order_system.return_case rc
                      JOIN order_system.mirakl_returns mr ON mr.order_id = rc.order_id
                      WHERE rc.shop_sku=%s AND rc.store=%s
                        AND mr.date_created >= DATE_SUB(NOW(), INTERVAL 90 DAY)
                      GROUP BY mr.reason_code) x""", (sku, store))
                reasons = (r or {}).get("d") or ""

                ours = _our_listing(headers, conn, store, sku)
                if not ours or not (ours["title"] or ours["long_desc"]):
                    stats["skipped"].append(f"{sku}@{store}:飞书无文案")
                    continue
                sup = _supplier_listing(headers, ours["supplier"] or t["supplier"],
                                        ours["supplier_sku"])
                if not sup:
                    stats["skipped"].append(f"{sku}@{store}:供应商资料缺({ours['supplier']}/{ours['supplier_sku']})")
                    continue
                prices = _prices(conn, sku, store, ours["supplier"] or t["supplier"],
                                 ours["supplier_sku"], sup.get("price"), ours)

                ai = _ai_compare(ours, sup, prices, reasons)
                verdict = (ai.get("verdict") or "minor").lower()
                if verdict not in ("clean", "minor", "severe"):
                    verdict = "minor"
                stats[verdict] += 1
                stats["analyzed"] += 1

                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO order_system.listing_sentinel_findings
                          (audit_date, store, operator, supplier, shop_sku, supplier_sku,
                           returns_recent, reason_dist, price_ours, price_supplier, price_ratio,
                           verdict, summary, issues_json, image_note,
                           our_title, supplier_title, item_link)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON DUPLICATE KEY UPDATE
                          audit_date=VALUES(audit_date), returns_recent=VALUES(returns_recent),
                          reason_dist=VALUES(reason_dist), price_ours=VALUES(price_ours),
                          price_supplier=VALUES(price_supplier), price_ratio=VALUES(price_ratio),
                          verdict=VALUES(verdict), summary=VALUES(summary),
                          issues_json=VALUES(issues_json), image_note=VALUES(image_note),
                          our_title=VALUES(our_title), supplier_title=VALUES(supplier_title),
                          item_link=VALUES(item_link),
                          status=IF(status='fixed' AND VALUES(verdict)<>'clean','open',status)
                    """, (date.today(), store, t["operator"], ours["supplier"] or t["supplier"],
                          sku, ours["supplier_sku"], int(t["n"]), reasons[:250],
                          prices["ours"], prices["supplier"], prices["ratio"],
                          verdict, (ai.get("summary") or "")[:1000],
                          json.dumps(ai.get("issues") or [], ensure_ascii=False),
                          (ai.get("image_note") or "")[:480],
                          (ours["title"] or "")[:480], (sup["title"] or "")[:480],
                          (sup.get("link") or "")[:480]))
                    if verdict == "severe":
                        cur.execute("""
                            INSERT INTO order_system.issue_log
                              (detected_date, issue_type, entity, severity, impact_usd,
                               evidence, suggestion)
                            VALUES (%s,'listing_mismatch',%s,'high',0,%s,%s)
                            ON DUPLICATE KEY UPDATE evidence=VALUES(evidence)
                        """, (date.today(), f"{sku}@{store}",
                              (ai.get("summary") or "")[:800],
                              "对照哨兵页的原文对照修正listing/图片，改完在哨兵页标记已修复"))
                conn.commit()
                time.sleep(0.3)
            except Exception as exc:
                stats["errors"].append(f"{sku}@{store}: {exc}")
                continue
    finally:
        conn.close()
    stats["elapsed_sec"] = round(time.time() - started, 1)
    return stats
