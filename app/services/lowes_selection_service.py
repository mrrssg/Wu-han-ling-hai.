# -*- coding: utf-8 -*-
"""Lowes 选品候选池（Autool=豪雅 / Yasonic=司顺，2026-07-23）。

单店重建：每次只重建一个店铺的候选。
  autool  → 豪雅Costway产品，品牌Volenca，已上过看 Lowes-Autool-Mirakl
  yasonic → 司顺Vevor产品，品牌Mecale，已上过看 Lowes-Yasonic-Mirakl
筛选：库存>50 + 没上过 + 供应商类目映射到了 Lowes 叶子(lowes_cat_map)。
候选存 lowes_selection_pool（带store区分），页面读它，勾选后推送到对应 Mirakl 表。
"""
from typing import Any, Dict, List

from app.models.db_manager import DBManager

_APP_ID = "cli_a940a2a1067adbd2"
_SECRET = "i2mKLGVzUDmu4v0U9HYEYdMGc0ZvZAgU"
_APP = "QEeubiXYGa83zXs3Zt8cSSJPnih"

# 店铺配置：供应商 / 品牌 / 已上过Mirakl表 / 推送目标表(同一张)
STORE_CFG = {
    "autool": {"supplier": "Costway", "brand": "Volenca", "mirakl": "tblGp3uvtOe99vjY"},
    "yasonic": {"supplier": "Vevor", "brand": "Mecale", "mirakl": "tbldeuRJOoJBfX2g"},
}


def _feishu_token() -> str:
    import requests
    return requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": _APP_ID, "app_secret": _SECRET}, timeout=30
    ).json()["tenant_access_token"]


def _gt(v):
    if isinstance(v, str):
        return v
    if isinstance(v, list) and v and isinstance(v[0], dict):
        return "".join(x.get("text", "") or x.get("link", "") for x in v)
    if isinstance(v, dict):
        return v.get("text") or v.get("link") or ""
    return str(v) if v is not None else ""


def _feishu_used_skus(mirakl_tbl: str) -> set:
    """某个 Lowes Mirakl 表的「供应商SKU」全集 = 该店已上过。"""
    import requests
    tok = _feishu_token()
    H = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    used = set()
    pt = ""
    while True:
        url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{_APP}"
               f"/tables/{mirakl_tbl}/records?page_size=500" + (f"&page_token={pt}" if pt else ""))
        r = requests.get(url, headers=H, timeout=60).json()
        d = r.get("data") or {}
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
    tok = _feishu_token()
    H = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    have = set()
    pt = ""
    while True:
        url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{_APP}"
               f"/tables/{TBL}/records?page_size=500" + (f"&page_token={pt}" if pt else ""))
        r = requests.get(url, headers=H, timeout=60).json()
        d = r.get("data") or {}
        for it in d.get("items") or []:
            f = it["fields"]
            sku = _gt(f.get("SKU")).strip()
            img = _gt(f.get("主图")).strip() or _gt(f.get("第1张")).strip()
            if sku and img.startswith("http"):
                have.add(sku)
        if not d.get("has_more"):
            break
        pt = d.get("page_token") or ""
        if not pt:
            break
    return have


def rebuild_pool(store: str) -> Dict[str, Any]:
    """只重建 store（autool/yasonic）一个店铺的候选池。"""
    cfg = STORE_CFG.get(store)
    if not cfg:
        return {"error": f"未知店铺 {store}"}
    supplier = cfg["supplier"]
    brand = cfg["brand"]
    used = _feishu_used_skus(cfg["mirakl"])
    overview = _feishu_overview_skus()

    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            # 有效映射(供应商类目 → Lowes完整路径)
            cur.execute("""SELECT supplier_cat, lowes_leaf, lowes_path
                           FROM order_system.lowes_cat_map
                           WHERE supplier=%s AND lowes_path IS NOT NULL""", (supplier,))
            cat2path = {r["supplier_cat"]: (r["lowes_leaf"], r["lowes_path"])
                        for r in cur.fetchall()}

            # 已上过灌临时表
            cur.execute("DROP TEMPORARY TABLE IF EXISTS _lused")
            cur.execute("CREATE TEMPORARY TABLE _lused "
                        "(sku VARCHAR(64) COLLATE utf8mb4_general_ci PRIMARY KEY)")
            ul = [s[:64] for s in used if s]
            for i in range(0, len(ul), 2000):
                c = ul[i:i + 2000]
                cur.execute(f"INSERT IGNORE INTO _lused (sku) VALUES {','.join(['(%s)']*len(c))}", c)

            if supplier == "Costway":
                cur.execute("""
                    SELECT c.sku, c.title, c.image_url AS img, d.Stock AS stock,
                           c.category AS cat, d.Price AS price
                    FROM order_system.safety_product_cache c
                    JOIN autooperate.newestdropship d ON d.SKU=c.sku
                    LEFT JOIN _lused u ON u.sku=c.sku COLLATE utf8mb4_general_ci
                    WHERE c.supplier='Costway' AND c.category<>'' AND d.Stock>50
                      AND u.sku IS NULL""")
            else:
                cur.execute("""
                    SELECT v.sku, v.title, v.image AS img, v.inventory AS stock,
                           v.product_type AS cat, v.price
                    FROM autooperate.vevor_feed v
                    LEFT JOIN _lused u ON u.sku=v.sku COLLATE utf8mb4_general_ci
                    WHERE v.product_type<>'' AND v.inventory>50 AND u.sku IS NULL""")
            recs = cur.fetchall()

        rows = []
        for r in recs:
            lp = cat2path.get(r["cat"])
            if not lp:
                continue   # 供应商类目没映射到Lowes叶子 → 不进池
            leaf, path = lp
            has_img = 1 if r["sku"] in overview else 0
            rows.append((store, supplier, r["sku"], (r.get("title") or "")[:400],
                         (r.get("img") or "")[:600], int(r.get("stock") or 0),
                         (r["cat"] or "")[:400], leaf, path, brand,
                         (str(r.get("price") or ""))[:32], 0, has_img))

        with conn.cursor() as cur:
            cur.execute("DELETE FROM order_system.lowes_selection_pool WHERE store=%s", (store,))
            cols = ("store,supplier,supplier_sku,title,image,stock,supplier_cat,"
                    "lowes_leaf,lowes_path,brand,price,heat_90d,has_overview_img,rebuilt_at")
            for i in range(0, len(rows), 1000):
                chunk = rows[i:i + 1000]
                ph = ",".join(["(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())"] * len(chunk))
                flat = [v for row in chunk for v in row]
                cur.execute(f"INSERT INTO order_system.lowes_selection_pool ({cols}) VALUES {ph}", flat)
        conn.commit()
        return {"store": store, "supplier": supplier, "used_skus": len(used),
                "mapped_cats": len(cat2path), "candidates": len(rows),
                "with_overview_img": sum(1 for r in rows if r[12])}
    finally:
        conn.close()


def push_to_feishu(pool_ids: List[int], batch_desc: str) -> Dict[str, Any]:
    """勾中的候选 → 对应店铺 Lowes-Mirakl 表新增行。按 store 分组分别推。"""
    import json
    import requests
    if not pool_ids:
        return {"success": False, "msg": "没有勾选"}
    conn = DBManager.get_connection()
    try:
        ph = ",".join(["%s"] * len(pool_ids))
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM order_system.lowes_selection_pool WHERE id IN ({ph})",
                        pool_ids)
            items = cur.fetchall()
    finally:
        conn.close()
    if not items:
        return {"success": False, "msg": "候选已失效，请重建后再选"}

    tok = _feishu_token()
    H = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    import re as _re

    def _price_num(s):
        m = _re.search(r"[\d.]+", str(s or ""))
        return float(m.group()) if m else None

    # 按 store 分组
    by_store: Dict[str, list] = {}
    for it in items:
        by_store.setdefault(it["store"], []).append(it)

    total_ok = 0
    per_store = {}
    for store, its in by_store.items():
        cfg = STORE_CFG.get(store)
        if not cfg:
            continue
        records = []
        for it in its:
            f = {
                "供应商SKU": it["supplier_sku"],
                "Item Name": it["title"] or "",
                "供应商类目": it["supplier_cat"] or "",
                "店铺类目": it["lowes_path"] or "",     # 完整Lowes路径
                "品牌": it["brand"] or cfg["brand"],
                "选品批次描述": batch_desc,
            }
            if it.get("stock") is not None:
                f["Stock"] = int(it["stock"])
            pn = _price_num(it.get("price"))
            if pn is not None:
                f["供应商价格"] = pn
            sup = {"Costway": "Costway", "Vevor": "Vevor"}.get(it["supplier"])
            if sup:
                f["供应商"] = sup
            records.append({"fields": f})

        ok = 0
        for i in range(0, len(records), 100):
            chunk = records[i:i + 100]
            r = requests.post(
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{_APP}/tables/{cfg['mirakl']}/records/batch_create",
                headers=H, data=json.dumps({"records": chunk}).encode("utf-8"), timeout=60).json()
            if r.get("code") == 0:
                ok += len(chunk)
        total_ok += ok
        per_store[store] = ok

        # 落推送记录
        if ok > 0:
            from collections import Counter
            leaf_c = Counter(it["lowes_leaf"] or "?" for it in its)
            leaf_summary = "; ".join(f"{k}×{v}" for k, v in leaf_c.most_common())
            conn = DBManager.get_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute("""INSERT INTO order_system.lowes_push_log
                        (store, batch_desc, sku_count, leaf_summary)
                        VALUES (%s,%s,%s,%s)""",
                        (store, batch_desc, ok, leaf_summary[:1000]))
                conn.commit()
            finally:
                conn.close()

    return {"success": total_ok > 0, "pushed": total_ok, "per_store": per_store,
            "batch": batch_desc}
