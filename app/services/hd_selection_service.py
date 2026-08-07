# -*- coding: utf-8 -*-
"""HD(Home Depot)选品候选池。TOP=厨卫/小家电、BOS=户外/庭院。

每天重算：从 Costway+Vevor 全量产品里筛出
  库存>50 + 没上过(两张HD飞书表供应商SKU并集,同平台跨店去重) + 供应商类目映射到了HD可上类目
的候选，存 hd_selection_pool。类目映射来自现有飞书记录(record一致=精选/conflict多落点=擦边),
未映射的走「未归类复核桶」人工补(写 hd_cat_override/hd_selection_decision)。
"""
from datetime import date, timedelta
from typing import Any, Dict, List

from app.models.db_manager import DBManager

# HD 两店飞书表(HD-TOP-Mirkal / HD-Boson-Mirkal);同平台,已上过=两表并集
HD_TABLES = {"top": "tblxHsORDrH6Ldvr", "bos": "tbl4OAnBZliXZ0Lm"}
NEWNESS_DAYS = 14
APP = "QEeubiXYGa83zXs3Zt8cSSJPnih"
APP_ID = "cli_a940a2a1067adbd2"
SECRET = "i2mKLGVzUDmu4v0U9HYEYdMGc0ZvZAgU"


def _token():
    import requests
    return requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": APP_ID, "app_secret": SECRET}, timeout=30,
    ).json()["tenant_access_token"]


def _gt(v):
    if isinstance(v, str):
        return v
    if isinstance(v, list) and v and isinstance(v[0], dict):
        return "".join(x.get("text", "") for x in v)
    if isinstance(v, dict):
        return v.get("text") or ""
    return str(v) if v is not None else ""


def _feishu_used_skus() -> set:
    """已上过=**两张HD飞书表(TOP+BOS)供应商SKU并集**(同平台跨店去重:同产品不能同时上两个HD店)。"""
    import requests
    H = {"Authorization": f"Bearer {_token()}"}
    used = set()
    for tbl in HD_TABLES.values():
        pt = ""
        while True:
            url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP}/tables/{tbl}/records"
                   f"?page_size=500" + (f"&page_token={pt}" if pt else ""))
            d = (requests.get(url, headers=H, timeout=60).json().get("data") or {})
            for it in d.get("items") or []:
                s = _gt(it["fields"].get("供应商SKU")).strip()
                if s:
                    used.add(s)
            if not d.get("has_more"):
                break
            pt = d.get("page_token") or ""
            if not pt:
                break
    return used


def _feishu_overview_skus() -> set:
    """图片总览表 tbl2IRXCLuiUBfk9 里「有主图或第1张」的 SKU 集合(有图=能上架取图)。"""
    import requests
    TBL = "tbl2IRXCLuiUBfk9"
    H = {"Authorization": f"Bearer {_token()}"}
    have, pt = set(), ""
    while True:
        url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP}/tables/{TBL}/records"
               f"?page_size=500" + (f"&page_token={pt}" if pt else ""))
        d = (requests.get(url, headers=H, timeout=60).json().get("data") or {})
        for it in d.get("items") or []:
            f = it["fields"]
            sku = _gt(f.get("SKU") or f.get("供应商SKU")).strip()
            if sku and (f.get("主图") or f.get("第1张") or f.get("图片")):
                have.add(sku)
        if not d.get("has_more"):
            break
        pt = d.get("page_token") or ""
        if not pt:
            break
    return have


def rebuild_pool(store: str = "top") -> Dict[str, Any]:
    store = store if store in HD_TABLES else "top"
    used = _feishu_used_skus()
    try:
        overview = _feishu_overview_skus()
    except Exception:
        overview = set()
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            # 本地已推(所有HD店,平台内跨店去重) + 飞书并集
            cur.execute("SELECT supplier_sku FROM order_system.hd_pushed_sku")
            used |= {r["supplier_sku"] for r in cur.fetchall() if r["supplier_sku"]}
            # 供应商类目 → HD类目 映射(record一致=精选 / conflict多落点=擦边)
            cur.execute("SELECT supplier, supplier_cat, hd_path, tier FROM order_system.hd_cat_map "
                        "WHERE store=%s AND hd_path IS NOT NULL", (store,))
            cat2path = {(r["supplier"], r["supplier_cat"]): (r["hd_path"], r["tier"])
                        for r in cur.fetchall()}
            # 人工决策 + 记住映射
            cur.execute("SELECT supplier, supplier_sku, decision, override_leaf, override_brand "
                        "FROM order_system.hd_selection_decision WHERE store=%s", (store,))
            sku_decision = {(r["supplier"], r["supplier_sku"]): r for r in cur.fetchall()}
            cur.execute("SELECT supplier, supplier_cat, override_leaf, override_brand "
                        "FROM order_system.hd_cat_override WHERE store=%s", (store,))
            cat_override = {(r["supplier"], r["supplier_cat"]): r for r in cur.fetchall()}

            cur.execute("DROP TEMPORARY TABLE IF EXISTS _hused")
            cur.execute("CREATE TEMPORARY TABLE _hused "
                        "(sku VARCHAR(64) COLLATE utf8mb4_general_ci PRIMARY KEY)")
            ul = [s[:64] for s in used if s]
            for i in range(0, len(ul), 2000):
                c = ul[i:i + 2000]
                cur.execute(f"INSERT IGNORE INTO _hused (sku) VALUES {','.join(['(%s)']*len(c))}", c)

            cur.execute("""
                SELECT c.sku, c.title, c.image_url AS img, d.Stock AS stock,
                       c.category AS cat, d.Price AS price, d.first_seen, d.restock_at
                FROM order_system.safety_product_cache c
                JOIN autooperate.newestdropship d ON d.SKU=c.sku
                LEFT JOIN _hused u ON u.sku=c.sku COLLATE utf8mb4_general_ci
                WHERE c.supplier='Costway' AND c.category<>'' AND d.Stock>50 AND u.sku IS NULL
                  AND COALESCE(d.status,'Enabled')<>'Disabled'""")
            cw = cur.fetchall()
            cur.execute("""
                SELECT v.sku, v.title, v.image AS img, v.inventory AS stock,
                       v.product_type AS cat, v.price, v.first_seen, v.restock_at
                FROM autooperate.vevor_feed v
                LEFT JOIN _hused u ON u.sku=v.sku COLLATE utf8mb4_general_ci
                WHERE v.product_type<>'' AND v.inventory>50 AND u.sku IS NULL""")
            vv = cur.fetchall()

        cutoff = date.today() - timedelta(days=NEWNESS_DAYS)
        rows = []
        for supplier, recs in (("Costway", cw), ("Vevor", vv)):
            for r in recs:
                dec = sku_decision.get((supplier, r["sku"]))
                if dec and dec["decision"] == "rejected":
                    continue
                ov = cat_override.get((supplier, r["cat"]))
                if ov:
                    hd_path = ov["override_leaf"]
                    brand = ov.get("override_brand")
                    tier, reason = "ai", "人工锁定类目"
                else:
                    lb = cat2path.get((supplier, r["cat"]))
                    if lb:
                        hd_path, maptier = lb
                        tier = "ai" if maptier == "record" else "manual"
                        reason = "现有记录一致" if maptier == "record" else "供应商类目落多个HD类目→擦边确认"
                        brand = None
                    else:
                        hd_path = brand = tier = reason = None
                if dec and dec["decision"] == "approved":
                    if dec.get("override_leaf"):
                        hd_path = dec["override_leaf"]
                        brand = dec.get("override_brand") or brand
                    if hd_path:
                        tier, reason = "ai", "人工采用进精选"
                if not hd_path:
                    continue
                has_img = 1 if r["sku"] in overview else 0
                fs, rs = r.get("first_seen"), r.get("restock_at")
                is_new = 1 if (fs and fs >= cutoff) else 0
                is_restock = 1 if (not is_new and rs and rs >= cutoff) else 0
                rows.append((store, tier, (reason or "")[:200], supplier, r["sku"],
                             (r.get("title") or "")[:500], (r.get("img") or "")[:600],
                             int(r.get("stock") or 0), (r["cat"] or "")[:400], hd_path,
                             (brand or "")[:48], (str(r.get("price") or ""))[:32],
                             0, has_img, is_new, is_restock))

        with conn.cursor() as cur:
            cur.execute("DELETE FROM order_system.hd_selection_pool WHERE store=%s", (store,))
            cols = ("store,tier,classify_reason,supplier,supplier_sku,title,image,stock,supplier_cat,"
                    "hd_path,brand,price,heat_90d,has_overview_img,is_new,is_restock,rebuilt_at")
            for i in range(0, len(rows), 1000):
                chunk = rows[i:i + 1000]
                ph = ",".join(["(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())"] * len(chunk))
                cur.execute(f"INSERT INTO order_system.hd_selection_pool ({cols}) VALUES {ph}",
                            [v for row in chunk for v in row])
            # 已上过快照(未归类桶用)
            cur.execute("DELETE FROM order_system.hd_used_sku WHERE store=%s", (store,))
            for i in range(0, len(ul), 2000):
                c = ul[i:i + 2000]
                cur.execute(f"INSERT IGNORE INTO order_system.hd_used_sku (store, supplier_sku) "
                            f"VALUES {','.join(['(%s,%s)'] * len(c))}", [v for s in c for v in (store, s)])
        conn.commit()
        return {"store": store, "used_skus": len(used), "overview_skus": len(overview),
                "candidates": len(rows),
                "ai": sum(1 for r in rows if r[1] == "ai"),
                "manual": sum(1 for r in rows if r[1] == "manual"),
                "costway": sum(1 for r in rows if r[3] == "Costway"),
                "vevor": sum(1 for r in rows if r[3] == "Vevor")}
    finally:
        conn.close()


def push_to_feishu(pool_ids: List[int], batch_desc: str) -> Dict[str, Any]:
    """勾中的候选 → 该HD店飞书表新增行，写供应商SKU/供应商/产品名/库存/供应商类目/店铺类目/批次。"""
    import json
    import re as _re
    import requests
    from collections import Counter
    if not pool_ids:
        return {"success": False, "msg": "没有勾选"}
    conn = DBManager.get_connection()
    try:
        ph = ",".join(["%s"] * len(pool_ids))
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM order_system.hd_selection_pool WHERE id IN ({ph})", pool_ids)
            items = cur.fetchall()
    finally:
        conn.close()
    if not items:
        return {"success": False, "msg": "候选不存在"}
    store = (items[0].get("store") or "top")
    TARGET = HD_TABLES.get(store, HD_TABLES["top"])
    H = {"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"}

    def _price_num(s):
        m = _re.search(r"[\d.]+", str(s or ""))
        return float(m.group()) if m else None

    records = []
    for it in items:
        f = {
            "供应商SKU": it["supplier_sku"],
            "Item Name": it["title"] or "",
            "供应商类目": it["supplier_cat"] or "",
            "店铺类目": it["hd_path"] or "",
            "选品批次描述": batch_desc,
        }
        if it.get("brand"):
            f["品牌"] = it["brand"]
        if it.get("stock") is not None:
            f["Stock"] = int(it["stock"])
        pn = _price_num(it.get("price"))
        if pn is not None:
            f["供应商价格"] = pn
        sup = {"Costway": "Costway", "Vevor": "Vevor"}.get(it["supplier"])
        if sup:
            f["供应商"] = sup
        records.append({"fields": f})

    ok, pushed = 0, []
    for i in range(0, len(records), 100):
        chunk = records[i:i + 100]
        r = requests.post(
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP}/tables/{TARGET}/records/batch_create",
            headers=H, data=json.dumps({"records": chunk}).encode("utf-8"), timeout=60).json()
        if r.get("code") == 0:
            ok += len(chunk)
            for it in items[i:i + 100]:
                if it.get("supplier_sku"):
                    pushed.append((it["supplier_sku"][:64], store))
        else:
            return {"success": False, "msg": f"飞书写入失败: {str(r)[:200]}", "pushed": ok}

    if ok > 0:
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cur:
                for i in range(0, len(pushed), 500):
                    c = pushed[i:i + 500]
                    cur.execute("INSERT IGNORE INTO order_system.hd_pushed_sku (supplier_sku, store) VALUES "
                                + ",".join(["(%s,%s)"] * len(c)), [v for x in c for v in x])
                # 从池里移除已推
                pushed_skus = [p[0] for p in pushed]
                for i in range(0, len(pushed_skus), 500):
                    c = pushed_skus[i:i + 500]
                    cur.execute(f"DELETE FROM order_system.hd_selection_pool WHERE store=%s AND supplier_sku IN "
                                f"({','.join(['%s']*len(c))})", [store] + c)
                leaf_c = Counter(it["hd_path"] for it in items)
                summ = "; ".join(f"{(k or '').split('/')[-1]}×{v}" for k, v in leaf_c.most_common()[:20])
                cur.execute("INSERT INTO order_system.hd_push_log (store,batch_desc,sku_count,costway_n,vevor_n,leaf_summary) "
                            "VALUES (%s,%s,%s,%s,%s,%s)",
                            (store, batch_desc[:200], ok,
                             sum(1 for it in items if it["supplier"] == "Costway"),
                             sum(1 for it in items if it["supplier"] == "Vevor"), summ[:500]))
            conn.commit()
        finally:
            conn.close()
    return {"success": True, "pushed": ok, "msg": f"已推送 {ok} 个到 HD-{store.upper()} 飞书表"}
