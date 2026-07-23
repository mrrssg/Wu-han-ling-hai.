# -*- coding: utf-8 -*-
"""Macy-Kuyotq 选品候选池（2026-07-23）。

每天重算：从两个供应商全量产品里筛出
  库存>50 + 没上过(飞书两张Mirakl表供应商SKU) + 供应商类目映射到了有效Macy叶子类目
的候选，存 macy_selection_pool（页面读它，勾选后推送飞书）。
"""
from typing import Any, Dict, List

from app.models.db_manager import DBManager


def _feishu_used_skus() -> set:
    """飞书 Macy-kuyotq-Mirakl + Macy-wopet-Mirakl 两表的「供应商SKU」全集=已上过。"""
    import requests
    APP_ID = "cli_a940a2a1067adbd2"
    SECRET = "i2mKLGVzUDmu4v0U9HYEYdMGc0ZvZAgU"
    APP = "QEeubiXYGa83zXs3Zt8cSSJPnih"
    tok = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": APP_ID, "app_secret": SECRET}, timeout=30
    ).json()["tenant_access_token"]
    H = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}

    def gt(v):
        if isinstance(v, str):
            return v
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return "".join(x.get("text", "") for x in v)
        if isinstance(v, dict):
            return v.get("text") or ""
        return str(v) if v is not None else ""

    used = set()
    for tbl in ("tblfyStm2eu3hp1Q", "tbla2i1OwdwlCweK"):
        pt = ""
        while True:
            url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP}"
                   f"/tables/{tbl}/records?page_size=500" + (f"&page_token={pt}" if pt else ""))
            r = requests.get(url, headers=H, timeout=60).json()
            d = r.get("data") or {}
            for it in d.get("items") or []:
                s = gt(it["fields"].get("供应商SKU")).strip()
                if s:
                    used.add(s)
            if not d.get("has_more"):
                break
            pt = d.get("page_token") or ""
            if not pt:
                break
    return used


DDL = """
CREATE TABLE IF NOT EXISTS order_system.macy_selection_pool (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    supplier VARCHAR(16),
    supplier_sku VARCHAR(64),
    title VARCHAR(400),
    image VARCHAR(600),
    stock INT,
    supplier_cat VARCHAR(400),
    macy_leaf VARCHAR(120),
    macy_brand VARCHAR(32),
    price VARCHAR(32),
    heat_90d INT DEFAULT 0 COMMENT '该Macy叶子类目近90天Kuyotq订单数',
    has_overview_img TINYINT DEFAULT 0 COMMENT '图片总览表tbl2IRXCLuiUBfk9里有此SKU的图',
    rebuilt_at DATETIME,
    UNIQUE KEY uq_sku (supplier, supplier_sku),
    KEY idx_leaf (macy_leaf), KEY idx_supplier (supplier)
) CHARSET=utf8mb4 COMMENT='Macy-Kuyotq选品候选池(每日重建)'
"""


def _feishu_overview_skus() -> set:
    """图片总览表 tbl2IRXCLuiUBfk9 里「有主图或第1张」的 SKU 集合(有图=能上架取图)。"""
    import requests
    APP_ID = "cli_a940a2a1067adbd2"
    SECRET = "i2mKLGVzUDmu4v0U9HYEYdMGc0ZvZAgU"
    APP = "QEeubiXYGa83zXs3Zt8cSSJPnih"
    TBL = "tbl2IRXCLuiUBfk9"
    tok = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": APP_ID, "app_secret": SECRET}, timeout=30
    ).json()["tenant_access_token"]
    H = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}

    def gt(v):
        if isinstance(v, str):
            return v
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return "".join(x.get("text", "") or x.get("link", "") for x in v)
        if isinstance(v, dict):
            return v.get("text") or v.get("link") or ""
        return str(v) if v is not None else ""

    have = set()
    pt = ""
    while True:
        url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP}"
               f"/tables/{TBL}/records?page_size=500" + (f"&page_token={pt}" if pt else ""))
        r = requests.get(url, headers=H, timeout=60).json()
        d = r.get("data") or {}
        for it in d.get("items") or []:
            f = it["fields"]
            sku = gt(f.get("SKU")).strip()
            img = gt(f.get("主图")).strip() or gt(f.get("第1张")).strip()
            if sku and img.startswith("http"):
                have.add(sku)
        if not d.get("has_more"):
            break
        pt = d.get("page_token") or ""
        if not pt:
            break
    return have


def rebuild_pool() -> Dict[str, Any]:
    used = _feishu_used_skus()
    overview = _feishu_overview_skus()
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
            try:
                cur.execute("ALTER TABLE order_system.macy_selection_pool "
                            "ADD COLUMN has_overview_img TINYINT DEFAULT 0")
            except Exception as exc:
                if "Duplicate column" not in str(exc):
                    raise
            # 有效映射(供应商类目→Macy叶子)
            cur.execute("""SELECT supplier, supplier_cat, macy_leaf, macy_brand
                           FROM order_system.macy_cat_map WHERE macy_leaf IS NOT NULL""")
            cat2leaf = {(r["supplier"], r["supplier_cat"]): (r["macy_leaf"], r["macy_brand"])
                        for r in cur.fetchall()}
            # 类目热度: 该Macy叶子近90天Kuyotq订单数（按offer的category粗匹配leaf名）
            cur.execute("""SELECT category, COUNT(DISTINCT order_id) AS n
                           FROM order_system.macy_order_data d
                           JOIN order_system.offerprice_listing o
                             ON o.shop_sku=d.offer_sku AND o.platform='Macy' AND o.shop_name='kuyotq'
                           WHERE d.order_state<>'CANCELED'
                             AND d.created_date>=DATE_SUB(CURDATE(),INTERVAL 90 DAY)
                           GROUP BY category""")
            heat = {(r["category"] or ""): int(r["n"] or 0) for r in cur.fetchall()}

            # 已上过灌临时表
            cur.execute("DROP TEMPORARY TABLE IF EXISTS _used")
            cur.execute("CREATE TEMPORARY TABLE _used "
                        "(sku VARCHAR(64) COLLATE utf8mb4_general_ci PRIMARY KEY)")
            ul = [s[:64] for s in used if s]
            for i in range(0, len(ul), 2000):
                c = ul[i:i + 2000]
                cur.execute(f"INSERT IGNORE INTO _used (sku) VALUES {','.join(['(%s)']*len(c))}", c)

            # Costway候选（带供应商价Price）
            cur.execute("""
                SELECT c.sku, c.title, c.image_url AS img, d.Stock AS stock,
                       c.category AS cat, d.Price AS price
                FROM order_system.safety_product_cache c
                JOIN autooperate.newestdropship d ON d.SKU=c.sku
                LEFT JOIN _used u ON u.sku=c.sku COLLATE utf8mb4_general_ci
                WHERE c.supplier='Costway' AND c.category<>'' AND d.Stock>50 AND u.sku IS NULL""")
            cw = cur.fetchall()
            cur.execute("""
                SELECT v.sku, v.title, v.image AS img, v.inventory AS stock,
                       v.product_type AS cat, v.price
                FROM autooperate.vevor_feed v
                LEFT JOIN _used u ON u.sku=v.sku COLLATE utf8mb4_general_ci
                WHERE v.product_type<>'' AND v.inventory>50 AND u.sku IS NULL""")
            vv = cur.fetchall()

        rows = []
        for supplier, recs in (("Costway", cw), ("Vevor", vv)):
            for r in recs:
                lb = cat2leaf.get((supplier, r["cat"]))
                if not lb:
                    continue   # 类目没映射到Macy叶子 → 不进池
                leaf, brand = lb
                has_img = 1 if r["sku"] in overview else 0
                rows.append((supplier, r["sku"], (r.get("title") or "")[:400],
                             (r.get("img") or "")[:600], int(r.get("stock") or 0),
                             (r["cat"] or "")[:400], leaf, brand,
                             (str(r.get("price") or ""))[:32], 0, has_img))

        with conn.cursor() as cur:
            cur.execute("DELETE FROM order_system.macy_selection_pool")
            cols = ("supplier,supplier_sku,title,image,stock,supplier_cat,"
                    "macy_leaf,macy_brand,price,heat_90d,has_overview_img,rebuilt_at")
            for i in range(0, len(rows), 1000):
                chunk = rows[i:i + 1000]
                ph = ",".join(["(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())"] * len(chunk))
                flat = [v for row in chunk for v in row]
                cur.execute(f"INSERT INTO order_system.macy_selection_pool ({cols}) VALUES {ph}", flat)
        conn.commit()
        return {"used_skus": len(used), "overview_skus": len(overview),
                "candidates": len(rows),
                "with_overview_img": sum(1 for r in rows if r[10]),
                "costway": sum(1 for r in rows if r[0] == "Costway"),
                "vevor": sum(1 for r in rows if r[0] == "Vevor")}
    finally:
        conn.close()


def push_to_feishu(pool_ids: List[int], batch_desc: str) -> Dict[str, Any]:
    """勾中的候选 → Macy-kuyotq-Mirakl 表新增行，写供应商SKU/供应商/产品名/库存/类目/品牌/选品批次描述。"""
    import json
    import requests
    APP = "QEeubiXYGa83zXs3Zt8cSSJPnih"
    KUYOTQ = "tblfyStm2eu3hp1Q"
    APP_ID = "cli_a940a2a1067adbd2"
    SECRET = "i2mKLGVzUDmu4v0U9HYEYdMGc0ZvZAgU"
    if not pool_ids:
        return {"success": False, "msg": "没有勾选"}
    conn = DBManager.get_connection()
    try:
        ph = ",".join(["%s"] * len(pool_ids))
        with conn.cursor() as cur:
            cur.execute(f"""SELECT * FROM order_system.macy_selection_pool
                            WHERE id IN ({ph})""", pool_ids)
            items = cur.fetchall()
            # 叶子类目 → 完整Macy类目路径（写「店铺类目」字段用）
            cur.execute("""SELECT brand, leaf, full_path FROM order_system.macy_leaf_category""")
            leaf_path = {(r["brand"], r["leaf"]): r["full_path"] for r in cur.fetchall()}
    finally:
        conn.close()
    tok = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": APP_ID, "app_secret": SECRET}, timeout=30
    ).json()["tenant_access_token"]
    H = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    # 供应商是单选(type3)，先探现有选项，值不在选项里就不写这个字段（防batch_create失败）
    import re as _re

    def _price_num(s):
        m = _re.search(r"[\d.]+", str(s or ""))
        return float(m.group()) if m else None

    records = []
    for it in items:
        full_path = leaf_path.get((it["macy_brand"], it["macy_leaf"])) or ""
        f = {
            "供应商SKU": it["supplier_sku"],
            "Item Name": it["title"] or "",
            "供应商类目": it["supplier_cat"] or "",
            "店铺类目": full_path,                 # 完整Macy类目路径
            "品牌": it["macy_brand"] or "",
            "选品批次描述": batch_desc,
        }
        if it.get("stock") is not None:
            f["Stock"] = int(it["stock"])
        pn = _price_num(it.get("price"))
        if pn is not None:
            f["供应商价格"] = pn
        # 供应商单选：Costway/Vevor 是表里已有的常见选项，直接写
        sup = {"Costway": "Costway", "Vevor": "Vevor"}.get(it["supplier"])
        if sup:
            f["供应商"] = sup
        records.append({"fields": f})
    ok = 0
    for i in range(0, len(records), 100):
        chunk = records[i:i + 100]
        r = requests.post(
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP}/tables/{KUYOTQ}/records/batch_create",
            headers=H, data=json.dumps({"records": chunk}).encode("utf-8"), timeout=60).json()
        if r.get("code") == 0:
            ok += len(chunk)
    return {"success": ok > 0, "pushed": ok, "batch": batch_desc}
