# -*- coding: utf-8 -*-
"""Lowes 选品候选池（Autool=豪雅 / Yasonic=司顺，2026-07-23）。

单店重建：每次只重建一个店铺的候选。
  autool  → 豪雅Costway产品，品牌Volenca，已上过看 Lowes-Autool-Mirakl
  yasonic → 司顺Vevor产品，品牌Mecale，已上过看 Lowes-Yasonic-Mirakl
筛选：库存>50 + 没上过 + 供应商类目映射到了 Lowes 叶子(lowes_cat_map)。
候选存 lowes_selection_pool（带store区分），页面读它，勾选后推送到对应 Mirakl 表。
"""
from datetime import date, datetime, timedelta
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
STORE_SHOP = {"autool": 10, "yasonic": 11}                 # lowes_order_data.shop_id
STORE_KEY = {"autool": "lowes_autool", "yasonic": "lowes_yasonic"}  # pricing_tier.store_key
CAT_GMV_WEIGHT = 0.5        # 类目分权重：GMV 与 净利率各半（用户2026-08-06定"两者加权"）
CAT_MARGIN_FULL = 0.20      # 净利率≥20%算满分（净口径，2026-08-06改：原毛利0.40）
LOWES_COMMISSION = 0.15     # Lowes平台佣金15%（autool/yasonic同，从毛利里扣）
RET_RATE_WINDOW = 180       # 退货率取近180天订单（够成熟，退货滞后~30-60天）
RET_RATE_MIN_ORDERS = 20    # 类目订单<20用店铺级退货率兜底（样本太少不可信）
NEWNESS_DAYS = 14           # first_seen/restock_at 在近14天内 → 新品/新库存(P2,只对Costway)
NEW_BONUS = 15              # 新品推荐分加成(封顶100)
RESTOCK_BONUS = 8           # 新库存加成


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


def _local_pushed_skus(cur, store: str) -> set:
    """本地已推镜像里该店的供应商SKU（补飞书同步延迟，刚推的立刻排除）。"""
    cur.execute("SELECT supplier_sku FROM order_system.lowes_pushed_sku WHERE store=%s", (store,))
    return {r["supplier_sku"] for r in cur.fetchall() if r["supplier_sku"]}


def _compute_category_demand(cur, store: str) -> Dict[str, int]:
    """近90天该店每个 Lowes 类目的 GMV + 真实净利率 → 归一化加权 score(0~100)。
    净利率 = 毛利(1-成本/售价) − 15%平台佣金 − 退货损失率；
      退货损失率 = 该类目退货率 × (1−毛利)  —— 全损店退一单亏整个成本、Lowes退货运费=0。
    高退货类目会被自动拉低甚至沉底(net可为负→拉低score)，避免推荐"高毛利高退货"坑品类。
    写 lowes_cat_demand(margin_rate=净利率, gross_rate=毛利, ret_rate=退货率)，返回 {leaf: score}。
    成本取 pricing_tier.cost_price，覆盖不到的行不计入毛利。"""
    shop_id = STORE_SHOP[store]
    store_key = STORE_KEY[store]
    # 1) 近90天 GMV / 毛利（成本×数量 ÷ 有成本行的成交额）
    cur.execute("""
        SELECT o.category_label AS leaf,
               SUM(o.line_total_price) AS gmv,
               SUM(o.quantity) AS units,
               SUM(COALESCE(pt.cost_price,0) * o.quantity) AS cost_sum,
               SUM(CASE WHEN pt.cost_price IS NOT NULL THEN o.line_total_price ELSE 0 END) AS gmv_c
        FROM order_system.lowes_order_data o
        LEFT JOIN order_system.pricing_tier pt
          ON pt.store_key=%s AND pt.shop_sku=o.offer_sku
        WHERE o.shop_id=%s AND o.order_state<>'CANCELED'
          AND o.created_date >= DATE_SUB(CURDATE(), INTERVAL 90 DAY)
          AND o.category_label IS NOT NULL AND o.category_label<>''
        GROUP BY o.category_label""", (store_key, shop_id))
    rows = cur.fetchall()
    if not rows:
        return {}
    # 2) 近180天各类目退货率(订单级) + 店铺级兜底(样本不足时用)
    cur.execute("""
        SELECT o.category_label AS leaf,
               COUNT(DISTINCT o.order_id) AS orders,
               COUNT(DISTINCT r.order_id) AS ret_orders
        FROM order_system.lowes_order_data o
        LEFT JOIN order_system.mirakl_returns r ON r.order_id = o.order_id
        WHERE o.shop_id=%s AND o.order_state<>'CANCELED'
          AND o.created_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
          AND o.category_label IS NOT NULL AND o.category_label<>''
        GROUP BY o.category_label""", (shop_id, RET_RATE_WINDOW))
    ret_map: Dict[str, float] = {}
    tot_ord = tot_ret = 0
    for x in cur.fetchall():
        o_n = int(x["orders"] or 0)
        r_n = int(x["ret_orders"] or 0)
        tot_ord += o_n
        tot_ret += r_n
        if o_n >= RET_RATE_MIN_ORDERS:
            ret_map[x["leaf"] or ""] = r_n / o_n if o_n else 0.0
    store_ret = (tot_ret / tot_ord) if tot_ord else 0.10   # 类目样本不足时的兜底退货率

    max_gmv = max(float(r["gmv"] or 0) for r in rows) or 1.0
    scores: Dict[str, int] = {}
    to_write = []
    for r in rows:
        gmv = float(r["gmv"] or 0)
        units = int(r["units"] or 0)
        gmv_c = float(r["gmv_c"] or 0)
        cost_sum = float(r["cost_sum"] or 0)
        gross = (1 - cost_sum / gmv_c) if gmv_c > 0 else None
        ret_rate = ret_map.get(r["leaf"] or "", store_ret)
        net = None if gross is None else gross - LOWES_COMMISSION - ret_rate * (1 - gross)
        gmv_norm = gmv / max_gmv
        net_norm = min(max((net if net is not None else 0) / CAT_MARGIN_FULL, -1.0), 1.0)
        score = round((CAT_GMV_WEIGHT * gmv_norm + (1 - CAT_GMV_WEIGHT) * net_norm) * 100)
        score = max(0, min(100, score))
        leaf = (r["leaf"] or "")[:120]
        scores[leaf] = score
        to_write.append((store, leaf, round(gmv, 2), units,
                         round(net, 4) if net is not None else None,
                         round(gross, 4) if gross is not None else None,
                         round(ret_rate, 4), score))
    # 季节列(season_tag/peak/trend_now/season_profile)由蓝海周刷(refresh_blue_ocean)维护，
    # 本重算是 DELETE+INSERT，会把它们冲成 NULL → 先快照、后恢复，别丢掉旺季数据。
    season_keep: Dict[str, Any] = {}
    try:
        cur.execute("SELECT lowes_leaf, season_tag, season_peak, trend_now, season_profile "
                    "FROM order_system.lowes_cat_demand WHERE store=%s", (store,))
        season_keep = {r["lowes_leaf"]: r for r in cur.fetchall()}
    except Exception:
        season_keep = {}          # 季节列还没建时忽略
    cur.execute("DELETE FROM order_system.lowes_cat_demand WHERE store=%s", (store,))
    for i in range(0, len(to_write), 500):
        c = to_write[i:i + 500]
        ph = ",".join(["(%s,%s,%s,%s,%s,%s,%s,%s,NOW())"] * len(c))
        cur.execute("INSERT INTO order_system.lowes_cat_demand "
                    "(store,lowes_leaf,gmv,units,margin_rate,gross_rate,ret_rate,score,computed_at) VALUES "
                    + ph, [v for row in c for v in row])
    for leaf in scores:           # 恢复季节列(只恢复仍存在的类目)
        sk = season_keep.get(leaf)
        if sk and sk.get("season_tag"):
            cur.execute("UPDATE order_system.lowes_cat_demand SET season_tag=%s, season_peak=%s,"
                        " trend_now=%s, season_profile=%s WHERE store=%s AND lowes_leaf=%s",
                        (sk["season_tag"], sk["season_peak"], sk["trend_now"],
                         sk["season_profile"], store, leaf))
    return scores


def rebuild_pool(store: str) -> Dict[str, Any]:
    """只重建 store（autool/yasonic）一个店铺的候选池。"""
    cfg = STORE_CFG.get(store)
    if not cfg:
        return {"error": f"未知店铺 {store}"}
    supplier = cfg["supplier"]
    brand = cfg["brand"]
    # Lowes 两店供应商不同(autool=Costway / yasonic=Vevor),SKU 天然不重叠,
    # 不存在"同一产品上两个Lowes店"的可能,故只按本店表去重即可。
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

            used |= _local_pushed_skus(cur, store)               # 叠加本地已推(补飞书同步延迟)
            cat_scores = _compute_category_demand(cur, store)    # 类目需求分(近90天GMV×毛利)

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
                           c.category AS cat, d.Price AS price,
                           d.first_seen, d.restock_at
                    FROM order_system.safety_product_cache c
                    JOIN autooperate.newestdropship d ON d.SKU=c.sku
                    LEFT JOIN _lused u ON u.sku=c.sku COLLATE utf8mb4_general_ci
                    WHERE c.supplier='Costway' AND c.category<>'' AND d.Stock>50
                      AND u.sku IS NULL
                      AND COALESCE(d.status,'Enabled')<>'Disabled'""")
            else:
                cur.execute("""
                    SELECT v.sku, v.title, v.image AS img, v.inventory AS stock,
                           v.product_type AS cat, v.price,
                           v.first_seen, v.restock_at
                    FROM autooperate.vevor_feed v
                    LEFT JOIN _lused u ON u.sku=v.sku COLLATE utf8mb4_general_ci
                    WHERE v.product_type<>'' AND v.inventory>50 AND u.sku IS NULL""")
            recs = cur.fetchall()

        cutoff = date.today() - timedelta(days=NEWNESS_DAYS)

        def _asdate(v):
            return v.date() if isinstance(v, datetime) else v   # date / None 原样

        rows = []
        for r in recs:
            lp = cat2path.get(r["cat"])
            if not lp:
                continue   # 供应商类目没映射到Lowes叶子 → 不进池
            leaf, path = lp
            has_img = 1 if r["sku"] in overview else 0
            fs = _asdate(r.get("first_seen")); rs = _asdate(r.get("restock_at"))
            is_new = 1 if (fs and fs >= cutoff) else 0
            is_restock = 1 if (not is_new and rs and rs >= cutoff) else 0
            base = int(cat_scores.get(leaf, 0))
            heat = min(100, base + (NEW_BONUS if is_new else RESTOCK_BONUS if is_restock else 0))
            rows.append((store, supplier, r["sku"], (r.get("title") or "")[:400],
                         (r.get("img") or "")[:600], int(r.get("stock") or 0),
                         (r["cat"] or "")[:400], leaf, path, brand,
                         (str(r.get("price") or ""))[:32],
                         heat, has_img, is_new, is_restock))

        with conn.cursor() as cur:
            cur.execute("DELETE FROM order_system.lowes_selection_pool WHERE store=%s", (store,))
            cols = ("store,supplier,supplier_sku,title,image,stock,supplier_cat,"
                    "lowes_leaf,lowes_path,brand,price,heat_90d,has_overview_img,"
                    "is_new,is_restock,rebuilt_at")
            for i in range(0, len(rows), 1000):
                chunk = rows[i:i + 1000]
                ph = ",".join(["(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())"] * len(chunk))
                flat = [v for row in chunk for v in row]
                cur.execute(f"INSERT INTO order_system.lowes_selection_pool ({cols}) VALUES {ph}", flat)
        conn.commit()
        return {"store": store, "supplier": supplier, "used_skus": len(used),
                "mapped_cats": len(cat2path), "scored_cats": len(cat_scores),
                "candidates": len(rows),
                "new": sum(1 for r in rows if r[13]),
                "restock": sum(1 for r in rows if r[14]),
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
                    # 本地已推镜像：立刻去重，不等飞书同步回来
                    cur.executemany("""INSERT INTO order_system.lowes_pushed_sku
                        (store, supplier_sku, supplier, batch_desc) VALUES (%s,%s,%s,%s)
                        ON DUPLICATE KEY UPDATE batch_desc=VALUES(batch_desc), pushed_at=NOW()""",
                        [(store, it["supplier_sku"], it["supplier"], batch_desc) for it in its])
                conn.commit()
            finally:
                conn.close()

    return {"success": total_ok > 0, "pushed": total_ok, "per_store": per_store,
            "batch": batch_desc}
