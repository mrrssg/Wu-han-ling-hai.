# -*- coding: utf-8 -*-
"""Macy-Kuyotq 选品候选池（2026-07-23）。

每天重算：从两个供应商全量产品里筛出
  库存>50 + 没上过(飞书两张Mirakl表供应商SKU) + 供应商类目映射到了有效Macy叶子类目
的候选，存 macy_selection_pool（页面读它，勾选后推送飞书）。
"""
from datetime import date, timedelta
from typing import Any, Dict, List

from app.models.db_manager import DBManager

# 类目推荐分：GMV 与 净利率各半（净利率=收入−实际佣金−成本,用 macy_order_data.commission_fee 真值）
CAT_GMV_WEIGHT = 0.5
CAT_MARGIN_FULL = 0.20      # 净利率≥20%算满分
NEWNESS_DAYS = 14           # first_seen/restock_at 在近14天 → 新品/新补货
NEW_BONUS = 15
RESTOCK_BONUS = 8
STORE_SHOP = {"kuyotq": "kuyotq", "wopet": "wopet"}   # store → offerprice_listing.shop_name


def _compute_macy_cat_demand(cur, store: str = "kuyotq") -> Dict[str, int]:
    """近90天该 Macy 店每类目 GMV + 净利率 → 归一化加权 score(0~100)。
    净利率 = (收入 − 实际佣金commission_fee − 成本last_cost_snapshot) / 收入，只按有成本的行算。
    写 macy_cat_demand，返回 {category_label(=macy_leaf): score}。"""
    shop = STORE_SHOP.get(store, "kuyotq")
    # 成本优先取补充表 macy_sku_cost(如 wopet 飞书回填),否则 offerprice_listing.last_cost_snapshot
    cur.execute("""
        SELECT o.category_label AS leaf,
               SUM(o.line_total_price) AS gmv,
               SUM(o.quantity) AS units,
               SUM(CASE WHEN COALESCE(sc.cost,l.last_cost_snapshot)>0 THEN o.line_total_price ELSE 0 END) AS gmv_c,
               SUM(CASE WHEN COALESCE(sc.cost,l.last_cost_snapshot)>0 THEN o.commission_fee ELSE 0 END) AS comm_c,
               SUM(CASE WHEN COALESCE(sc.cost,l.last_cost_snapshot)>0
                        THEN COALESCE(sc.cost,l.last_cost_snapshot)*o.quantity ELSE 0 END) AS cost_c
        FROM order_system.macy_order_data o
        JOIN order_system.offerprice_listing l
          ON l.shop_sku=o.offer_sku AND l.platform='Macy' AND l.shop_name=%s
        LEFT JOIN order_system.macy_sku_cost sc
          ON sc.store=%s AND sc.shop_sku=o.offer_sku
        WHERE o.order_state<>'CANCELED'
          AND o.created_date >= DATE_SUB(CURDATE(), INTERVAL 90 DAY)
          AND o.category_label IS NOT NULL AND o.category_label<>''
        GROUP BY o.category_label""", (shop, store))
    rows = cur.fetchall()
    if not rows:
        # 无订单也要清掉该店旧行(否则历史/误写的类目残留在推荐分面板)
        cur.execute("DELETE FROM order_system.macy_cat_demand WHERE store=%s", (store,))
        return {}
    max_gmv = max(float(r["gmv"] or 0) for r in rows) or 1.0
    scores: Dict[str, int] = {}
    to_write = []
    for r in rows:
        gmv = float(r["gmv"] or 0)
        units = int(r["units"] or 0)
        gmv_c = float(r["gmv_c"] or 0)
        comm_c = float(r["comm_c"] or 0)
        cost_c = float(r["cost_c"] or 0)
        if gmv_c > 0:
            net = (gmv_c - comm_c - cost_c) / gmv_c
            gross = 1 - cost_c / gmv_c
            comm_rate = comm_c / gmv_c
        else:
            net = gross = comm_rate = None
        gmv_norm = gmv / max_gmv
        net_norm = min(max((net if net is not None else 0) / CAT_MARGIN_FULL, -1.0), 1.0)
        score = max(0, min(100, round((CAT_GMV_WEIGHT * gmv_norm + (1 - CAT_GMV_WEIGHT) * net_norm) * 100)))
        leaf = (r["leaf"] or "")[:120]
        scores[leaf] = score
        to_write.append((store, leaf, round(gmv, 2), units,
                         round(net, 4) if net is not None else None,
                         round(gross, 4) if gross is not None else None,
                         round(comm_rate, 4) if comm_rate is not None else None, score))
    # 季节列由蓝海周刷(refresh_macy_blue_ocean)维护；本重算 DELETE+INSERT 会冲成 NULL
    # → 先快照、后恢复,别丢旺季数据。
    season_keep: Dict[str, Any] = {}
    try:
        cur.execute("SELECT macy_leaf, season_tag, season_peak, trend_now, season_profile "
                    "FROM order_system.macy_cat_demand WHERE store=%s", (store,))
        season_keep = {r["macy_leaf"]: r for r in cur.fetchall()}
    except Exception:
        season_keep = {}
    cur.execute("DELETE FROM order_system.macy_cat_demand WHERE store=%s", (store,))
    for i in range(0, len(to_write), 500):
        c = to_write[i:i + 500]
        ph = ",".join(["(%s,%s,%s,%s,%s,%s,%s,%s,NOW())"] * len(c))
        cur.execute("INSERT INTO order_system.macy_cat_demand "
                    "(store,macy_leaf,gmv,units,margin_rate,gross_rate,comm_rate,score,computed_at) VALUES "
                    + ph, [v for row in c for v in row])
    for leaf in scores:
        sk = season_keep.get(leaf)
        if sk and sk.get("season_tag"):
            cur.execute("UPDATE order_system.macy_cat_demand SET season_tag=%s, season_peak=%s,"
                        " trend_now=%s, season_profile=%s WHERE store=%s AND macy_leaf=%s",
                        (sk["season_tag"], sk["season_peak"], sk["trend_now"],
                         sk["season_profile"], store, leaf))
    return scores


STORE_MIRAKL = {"kuyotq": "tblfyStm2eu3hp1Q", "wopet": "tbla2i1OwdwlCweK"}
STORE_BRAND = {"kuyotq": None, "wopet": "COZITO"}   # wopet 固定 COZITO;kuyotq 品牌按类目映射


def _feishu_used_skus(store: str = "kuyotq") -> set:
    """该 Macy 店 Mirakl 表的「供应商SKU」全集=已上过(按店,不再混两店)。"""
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
    for tbl in (STORE_MIRAKL.get(store, "tblfyStm2eu3hp1Q"),):
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
    heat_90d INT DEFAULT 0 COMMENT '推荐分=该Macy叶子近90天净利率×GMV(+新品/补货加成)',
    has_overview_img TINYINT DEFAULT 0 COMMENT '图片总览表tbl2IRXCLuiUBfk9里有此SKU的图',
    is_new TINYINT DEFAULT 0 COMMENT '新品(first_seen近14天)',
    is_restock TINYINT DEFAULT 0 COMMENT '新补货(restock_at近14天)',
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


import re as _re

# kuyotq 擦边信号：AI 映射理由里出现"够不着精确叶子、只能取最近"的措辞 → 归人工待选池
_HEDGE_RE = _re.compile(r"最接近|最贴近|最贴合|勉强|近似|大致|可视为|暂归|无[^，。]{0,6}(细分|对应|匹配)|给定叶子")


def _kuyotq_tier(ai_reason: str):
    """kuyotq 按 cat_map 的 AI 理由分池：措辞在'取最近'一档 → ('manual', 擦边原因);否则精选。"""
    r = ai_reason or ""
    if _HEDGE_RE.search(r):
        return "manual", "类目非精确对应,AI取最近叶子(需人工确认): " + r[:150]
    return "ai", None


def _classify_wopet(supplier: str, title: str, supplier_cat: str):
    """wopet 逐产品分类器 → (macy_leaf, tier, reason) 或 (None,None,None)。
    tier: 'ai'=有把握精选 / 'manual'=擦边人工待选。宠物(6叶子)豪雅+司顺都收；Camping 只司顺。
    cat/dog/pet 用词边界匹配(避免 delicate/application/carpet 这类子串误命中)。"""
    import re
    t = f"{title or ''} {supplier_cat or ''}".lower()

    def has(*ws):
        return any(w in t for w in ws)

    def wb(*ws):
        return any(re.search(r"\b" + w + r"\b", t) for w in ws)

    # 猫（词边界 cat/cats/kitten/feline）
    if wb("cat", "cats", "kitten", "kittens", "kitty", "feline"):
        if has("litter", "cleaning", "scoop", "waste"):
            return "Cat Litter & Cleaning", "ai", "cat+litter"
        if has("tree", "condo", "scratch", "tower", "perch", "house", "furniture",
                "bed", "shelf", "climb", "cage", "window", "hammock", "cave"):
            return "Cat Furniture", "ai", "cat furniture词"
        return "Cat Furniture", "manual", "只识别到cat,细分不明→擦边"
    # 狗（词边界）
    if wb("dog", "dogs", "puppy", "puppies", "canine"):
        if has("crate", "kennel", "carrier", "cage", "playpen", "gate", "fence", "enclosure"):
            return "Dog Crates & Carriers & Gates", "ai", "dog+笼子/围栏"
        if has("collar", "leash", "harness"):
            return "Dog Collars & Leashes", "ai", "dog+项圈/牵引"
        if has("training", "muzzle", "clicker", "potty", "pee pad", "bark", "agility"):
            return "Dog Training", "ai", "dog+训练"
        if has("bed", "sofa", "couch", "mat", "cushion", "house", "furniture",
                "stairs", "steps", "ramp", "crib"):
            return "Dog Bedding & Furniture", "ai", "dog+床/家具"
        return "Dog Bedding & Furniture", "manual", "只识别到dog,细分不明→擦边"
    # 泛宠物(词边界 pet/pets,无猫狗) → 擦边;纯小动物/爬宠/鸟不是wopet类目→不收
    if wb("pet", "pets"):
        return "Dog Bedding & Furniture", "manual", "泛宠物无猫狗→擦边"
    # Camping —— 只司顺(Vevor)。Q1=B:只收真·露营,patio/grill/cooler 等不算(不进池,不进擦边)
    if supplier == "Vevor":
        if has("tent", "sleeping bag", "sleeping pad", "camping", "camp cot", "backpacking",
               "camp stove", "camping chair", "bivy", "camp table", "camp kitchen") \
                and not has("patio", "fire pit", "firepit", "grill", "cooler", "heater", "umbrella", "gazebo"):
            return "Camping & Outdoor Recreation Gear", "ai", "露营词"
        # 露营模糊(有 camp/背包但没帐篷/睡袋等明确词)→ 擦边(很窄)
        if has(" camp ", "backpack", "outdoor recreation", "hiking") \
                and not has("patio", "fire pit", "grill", "cooler", "heater", "umbrella", "chair", "table"):
            return "Camping & Outdoor Recreation Gear", "manual", "露营沾边不明→擦边"
    return None, None, None


def rebuild_pool(store: str = "kuyotq") -> Dict[str, Any]:
    used = _feishu_used_skus(store)
    overview = _feishu_overview_skus()
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
            for _alt in ("ADD COLUMN has_overview_img TINYINT DEFAULT 0",
                         "ADD COLUMN is_new TINYINT DEFAULT 0",
                         "ADD COLUMN is_restock TINYINT DEFAULT 0",
                         "ADD COLUMN store VARCHAR(12) NOT NULL DEFAULT 'kuyotq'",
                         "ADD COLUMN tier VARCHAR(8) NOT NULL DEFAULT 'ai'",
                         "ADD COLUMN classify_reason VARCHAR(200) DEFAULT NULL"):
                try:
                    cur.execute("ALTER TABLE order_system.macy_selection_pool " + _alt)
                except Exception as exc:
                    if "Duplicate column" not in str(exc):
                        raise
            # 本地推送明细并入"已上过"：刚推的SKU立刻排除，不依赖飞书传播/整表读全
            cur.execute("""CREATE TABLE IF NOT EXISTS order_system.macy_pushed_sku (
                supplier_sku VARCHAR(64) PRIMARY KEY,
                supplier VARCHAR(16), batch_desc VARCHAR(255),
                pushed_at DATETIME DEFAULT CURRENT_TIMESTAMP) CHARSET=utf8mb4""")
            cur.execute("SELECT supplier_sku FROM order_system.macy_pushed_sku")
            local_pushed = {r["supplier_sku"] for r in cur.fetchall() if r["supplier_sku"]}
            used |= local_pushed
            # 有效映射(供应商类目→Macy叶子);带AI理由,据此分精选/擦边两池
            cur.execute("""SELECT supplier, supplier_cat, macy_leaf, macy_brand, ai_reason
                           FROM order_system.macy_cat_map WHERE macy_leaf IS NOT NULL""")
            cat2leaf = {(r["supplier"], r["supplier_cat"]):
                        (r["macy_leaf"], r["macy_brand"], r.get("ai_reason") or "")
                        for r in cur.fetchall()}
            # 人工决策(擦边池)：逐SKU 采用/弃用/改类目 + 按供应商类目"记住映射"覆盖
            cur.execute("SELECT supplier, supplier_sku, decision, override_leaf, override_brand "
                        "FROM order_system.macy_selection_decision WHERE store=%s", (store,))
            sku_decision = {(r["supplier"], r["supplier_sku"]): r for r in cur.fetchall()}
            cur.execute("SELECT supplier, supplier_cat, override_leaf, override_brand "
                        "FROM order_system.macy_cat_override WHERE store=%s", (store,))
            cat_override = {(r["supplier"], r["supplier_cat"]): r for r in cur.fetchall()}
            # 类目推荐分：近90天净利率(收入−实际佣金−成本)×GMV,写 macy_cat_demand,返回 {leaf: score}
            cat_scores = _compute_macy_cat_demand(cur, store=store)

            # 已上过灌临时表
            cur.execute("DROP TEMPORARY TABLE IF EXISTS _used")
            cur.execute("CREATE TEMPORARY TABLE _used "
                        "(sku VARCHAR(64) COLLATE utf8mb4_general_ci PRIMARY KEY)")
            ul = [s[:64] for s in used if s]
            for i in range(0, len(ul), 2000):
                c = ul[i:i + 2000]
                cur.execute(f"INSERT IGNORE INTO _used (sku) VALUES {','.join(['(%s)']*len(c))}", c)

            # Costway候选（带供应商价Price + first_seen/restock新品判定；排除Disabled禁用品）
            cur.execute("""
                SELECT c.sku, c.title, c.image_url AS img, d.Stock AS stock,
                       c.category AS cat, d.Price AS price, d.first_seen, d.restock_at
                FROM order_system.safety_product_cache c
                JOIN autooperate.newestdropship d ON d.SKU=c.sku
                LEFT JOIN _used u ON u.sku=c.sku COLLATE utf8mb4_general_ci
                WHERE c.supplier='Costway' AND c.category<>'' AND d.Stock>50 AND u.sku IS NULL
                  AND COALESCE(d.status,'Enabled')<>'Disabled'""")
            cw = cur.fetchall()
            cur.execute("""
                SELECT v.sku, v.title, v.image AS img, v.inventory AS stock,
                       v.product_type AS cat, v.price, v.first_seen, v.restock_at
                FROM autooperate.vevor_feed v
                LEFT JOIN _used u ON u.sku=v.sku COLLATE utf8mb4_general_ci
                WHERE v.product_type<>'' AND v.inventory>50 AND u.sku IS NULL""")
            vv = cur.fetchall()

        cutoff = date.today() - timedelta(days=NEWNESS_DAYS)
        rows = []
        for supplier, recs in (("Costway", cw), ("Vevor", vv)):
            for r in recs:
                # 归类：kuyotq 用类目映射表(全 ai);wopet 用逐产品分类器(ai精选/manual擦边)
                if store == "wopet":
                    leaf, tier, reason = _classify_wopet(supplier, r.get("title"), r.get("cat"))
                    if not leaf:
                        continue
                    brand = STORE_BRAND["wopet"]
                else:
                    lb = cat2leaf.get((supplier, r["cat"]))
                    if not lb:
                        continue
                    leaf, brand, cat_reason = lb
                    tier, reason = _kuyotq_tier(cat_reason)
                # 人工"记住映射"(按供应商类目)——含将来新品自动跟,直接进精选
                ov = cat_override.get((supplier, r["cat"]))
                if ov:
                    leaf = ov["override_leaf"]
                    if ov.get("override_brand"):
                        brand = ov["override_brand"]
                    tier, reason = "ai", "人工锁定类目"
                # 人工逐SKU决策：弃用→剔除;采用→进精选(带改后类目)
                dec = sku_decision.get((supplier, r["sku"]))
                if dec:
                    if dec["decision"] == "rejected":
                        continue
                    tier = "ai"
                    if dec.get("override_leaf"):
                        leaf = dec["override_leaf"]
                    if dec.get("override_brand"):
                        brand = dec["override_brand"]
                    reason = "人工采用进精选"
                has_img = 1 if r["sku"] in overview else 0
                fs, rs = r.get("first_seen"), r.get("restock_at")
                is_new = 1 if (fs and fs >= cutoff) else 0
                is_restock = 1 if (not is_new and rs and rs >= cutoff) else 0
                base = cat_scores.get(leaf, 0)
                heat = min(100, base + (NEW_BONUS if is_new else RESTOCK_BONUS if is_restock else 0))
                rows.append((store, tier, (reason or "")[:200], supplier, r["sku"],
                             (r.get("title") or "")[:400], (r.get("img") or "")[:600],
                             int(r.get("stock") or 0), (r["cat"] or "")[:400], leaf, brand,
                             (str(r.get("price") or ""))[:32], heat, has_img, is_new, is_restock))

        with conn.cursor() as cur:
            cur.execute("DELETE FROM order_system.macy_selection_pool WHERE store=%s", (store,))
            cols = ("store,tier,classify_reason,supplier,supplier_sku,title,image,stock,supplier_cat,"
                    "macy_leaf,macy_brand,price,heat_90d,has_overview_img,is_new,is_restock,rebuilt_at")
            for i in range(0, len(rows), 1000):
                chunk = rows[i:i + 1000]
                ph = ",".join(["(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())"] * len(chunk))
                flat = [v for row in chunk for v in row]
                cur.execute(f"INSERT INTO order_system.macy_selection_pool ({cols}) VALUES {ph}", flat)
        conn.commit()
        return {"store": store, "used_skus": len(used), "local_pushed_skus": len(local_pushed),
                "overview_skus": len(overview), "candidates": len(rows),
                "ai": sum(1 for r in rows if r[1] == "ai"),
                "manual": sum(1 for r in rows if r[1] == "manual"),
                "costway": sum(1 for r in rows if r[3] == "Costway"),
                "vevor": sum(1 for r in rows if r[3] == "Vevor")}
    finally:
        conn.close()


def push_to_feishu(pool_ids: List[int], batch_desc: str) -> Dict[str, Any]:
    """勾中的候选 → Macy-kuyotq-Mirakl 表新增行，写供应商SKU/供应商/产品名/库存/类目/品牌/选品批次描述。"""
    import json
    import requests
    APP = "QEeubiXYGa83zXs3Zt8cSSJPnih"
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
            store = (items[0].get("store") if items else None) or "kuyotq"
            TARGET = STORE_MIRAKL.get(store, "tblfyStm2eu3hp1Q")
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
        full_path = leaf_path.get((it["macy_brand"], it["macy_leaf"])) or it["macy_leaf"] or ""
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
    pushed = []   # 成功推送的 (供应商SKU, 供应商, 批次)，落本地防飞书写入延迟/整表读取抖动漏排
    for i in range(0, len(records), 100):
        chunk = records[i:i + 100]
        r = requests.post(
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP}/tables/{TARGET}/records/batch_create",
            headers=H, data=json.dumps({"records": chunk}).encode("utf-8"), timeout=60).json()
        if r.get("code") == 0:
            ok += len(chunk)
            for it in items[i:i + 100]:
                if it.get("supplier_sku"):
                    pushed.append((it["supplier_sku"][:64], it["supplier"], batch_desc[:255]))

    # 落推送记录（推了什么类目/多少SKU/何时）
    if ok > 0:
        from collections import Counter
        leaf_c = Counter(f"{it['macy_brand']}|{it['macy_leaf']}" for it in items)
        leaf_summary = "; ".join(f"{k.split('|')[1]}×{v}" for k, v in leaf_c.most_common())
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""CREATE TABLE IF NOT EXISTS order_system.macy_push_log (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    batch_desc VARCHAR(255), sku_count INT,
                    costway_n INT, vevor_n INT,
                    leaf_summary VARCHAR(1000) COMMENT '类目×数量',
                    pushed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    KEY idx_at (pushed_at)) CHARSET=utf8mb4""")
                cur.execute("""INSERT INTO order_system.macy_push_log
                    (batch_desc, sku_count, costway_n, vevor_n, leaf_summary)
                    VALUES (%s,%s,%s,%s,%s)""",
                    (batch_desc, ok,
                     sum(1 for it in items if it["supplier"] == "Costway"),
                     sum(1 for it in items if it["supplier"] == "Vevor"),
                     leaf_summary[:1000]))
                # 本地推送明细：重建时并入"已上过"，刚推的SKU立刻被排除，不等飞书传播
                cur.execute("""CREATE TABLE IF NOT EXISTS order_system.macy_pushed_sku (
                    supplier_sku VARCHAR(64) PRIMARY KEY,
                    supplier VARCHAR(16), batch_desc VARCHAR(255),
                    pushed_at DATETIME DEFAULT CURRENT_TIMESTAMP) CHARSET=utf8mb4""")
                if pushed:
                    cur.executemany("""INSERT INTO order_system.macy_pushed_sku
                        (supplier_sku, supplier, batch_desc) VALUES (%s,%s,%s)
                        ON DUPLICATE KEY UPDATE batch_desc=VALUES(batch_desc),
                        pushed_at=CURRENT_TIMESTAMP""", pushed)
            conn.commit()
        finally:
            conn.close()
    return {"success": ok > 0, "pushed": ok, "batch": batch_desc}
