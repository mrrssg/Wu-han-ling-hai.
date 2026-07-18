from datetime import datetime

from flask import Blueprint, render_template, request, url_for
from app.models.db_manager import DBManager

# 定义一个名为 'main' 的蓝图
main_bp = Blueprint('main', __name__)


# 各店铺 cursor 不更新的告警阈值（小时）。
# lowes 店订单量极少,放宽到 7 天;其它店 6 小时。
STALE_THRESHOLD_HOURS = {
    "lowes_autool": 168,
    "lowes_yasonic": 168,
}
DEFAULT_STALE_HOURS = 6


def _q1(sql, params=None):
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params) if params else cur.execute(sql)
            return cur.fetchone()
    finally:
        conn.close()


def _qall(sql):
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall() or []
    finally:
        conn.close()


def _qall_p(sql, params):
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() or []
    finally:
        conn.close()


ISSUE_TYPE_NAMES = {
    "cell_below_baseline": "利润率破线",
    "negative_ev_sku": "负期望SKU",
    "recovery_overdue": "追款超期",
    "data_stale": "数据回填滞后",
    "return_spike": "退货异动",
    "delisted_but_selling": "已下架仍出单",
    "listing_mismatch": "Listing不符",
    "price_push_mismatch": "推价异常（未生效/被平台改动/下线）",
    "safety_risk": "产品安全风险（案例命中，在卖）",
}


@main_bp.route('/')
def index():
    """待办工作台：销售看飞书看板、利润看利润控制台，首页只放
    ①今天要处理的报警 ②未发货 ③利润一眼 ④常用入口。每项查询独立容错。"""
    todos = []      # {icon, label, count, money, href, danger}
    unshipped = []  # {label, shipping, waiting}
    returns_y = {"total": 0, "rows": []}   # 昨日退货 per 店铺

    # 首页十几条统计共享一条连接（逐条开连接≈5秒且容易在2个worker下堆积）
    conn = DBManager.get_connection()

    def q1(sql, params=None):
        with conn.cursor() as cur:
            cur.execute(sql, params) if params else cur.execute(sql)
            return cur.fetchone()

    def qall(sql, params=None):
        with conn.cursor() as cur:
            cur.execute(sql, params) if params else cur.execute(sql)
            return cur.fetchall() or []

    def _safe(fn):
        try:
            fn()
        except Exception:
            pass

    def _todo_unfiled():
        r = q1("""SELECT COUNT(*) AS n, COALESCE(SUM(exposure),0) AS v
                   FROM order_system.return_case
                   WHERE state='pending' AND supplier='Costway' AND cost >= 20
                     AND claim_filed=0 AND DATEDIFF(CURDATE(), order_date) <= 90""")
        if r and int(r["n"] or 0):
            todos.append({"icon": "❗", "label": "追款漏网（退货了还没登记）",
                          "count": int(r["n"]), "money": float(r["v"] or 0),
                          "href": url_for("profit_control.actions"), "danger": True})

    def _todo_near_writeoff():
        r = q1("""SELECT COUNT(*) AS n, COALESCE(SUM(exposure),0) AS v
                   FROM order_system.return_case
                   WHERE state='pending' AND supplier='Costway'
                     AND DATEDIFF(CURDATE(), return_date) >= 150""")
        if r and int(r["n"] or 0):
            todos.append({"icon": "⏰", "label": "追款临近180天核销线（等了≥150天）",
                          "count": int(r["n"]), "money": float(r["v"] or 0),
                          "href": url_for("profit_control.actions"), "danger": True})

    def _todo_sentinel():
        r = q1("""SELECT COUNT(*) AS n FROM order_system.listing_sentinel_findings
                   WHERE verdict='severe' AND status='open'""")
        if r and int(r["n"] or 0):
            todos.append({"icon": "🔴", "label": "Listing严重不符（哨兵发现，未修复）",
                          "count": int(r["n"]), "money": None,
                          "href": url_for("profit_control.sentinel"), "danger": True})

    def _todo_issues():
        rows = qall("""SELECT issue_type, COUNT(*) AS n, COALESCE(SUM(impact_usd),0) AS v
                        FROM order_system.issue_log WHERE status='open'
                        GROUP BY issue_type ORDER BY v DESC""")
        for r in rows:
            t = r["issue_type"]
            danger = t in ("delisted_but_selling", "price_push_mismatch", "safety_risk")
            todos.append({"icon": "🚨" if danger else "📋",
                          "label": ISSUE_TYPE_NAMES.get(t, t),
                          "count": int(r["n"] or 0), "money": float(r["v"] or 0),
                          "href": url_for("profit_control.issues"), "danger": danger})

    def _todo_repricing():
        # 待改价 = 每店最新 监控run(mon-)+分档定价run(plan-) 的 dry_run 去重合并，
        # 减去之后已 success 推价的（口径同候选页 _all_candidates）
        from app.services.repricing_stores import REPRICING_STORES
        for key, cfg in REPRICING_STORES.items():
            skus = set()
            for prefix in ("mon", "plan"):
                latest = q1(
                    """SELECT run_id FROM order_system.offer_price_change_log
                       WHERE run_id LIKE %s AND status='dry_run'
                       ORDER BY triggered_at DESC LIMIT 1""", (f"{prefix}-{key}-%",))
                if not latest:
                    continue
                rows = qall(
                    """SELECT log.shop_sku FROM order_system.offer_price_change_log log
                       WHERE log.run_id=%s AND log.status='dry_run'
                         AND NOT EXISTS (
                             SELECT 1 FROM order_system.offer_price_change_log later
                              WHERE later.shop_sku = log.shop_sku
                                AND later.store_key = %s
                                AND later.triggered_at > log.triggered_at
                                AND later.status = 'success')""",
                    (latest["run_id"], key))
                skus.update(r["shop_sku"] for r in rows)
            if skus:
                todos.append({"icon": "💲", "label": f"待改价（{cfg['label']}）",
                              "count": len(skus), "money": None,
                              "href": url_for("repricing.candidates_page", store=key),
                              "danger": False})

    def _unshipped():
        # 权威口径（用户 2026-07-15 定）：Mirakl同步表 order_state，
        # SHIPPING=已接单待发货，WAITING_ACCEPTANCE=还没接单（更急）。
        rows = qall("""
            SELECT sc.platform AS label, d.order_state, COUNT(*) AS n FROM (
                SELECT shop_id, order_state FROM order_system.macy_order_data
                 WHERE order_state IN ('SHIPPING','WAITING_ACCEPTANCE')
                UNION ALL
                SELECT shop_id, order_state FROM order_system.lowes_order_data
                 WHERE order_state IN ('SHIPPING','WAITING_ACCEPTANCE')
                UNION ALL
                SELECT shop_id, order_state FROM order_system.bestbuy_order_data
                 WHERE order_state IN ('SHIPPING','WAITING_ACCEPTANCE')
            ) d JOIN order_system.shop_configs sc ON sc.id = d.shop_id
            GROUP BY sc.platform, d.order_state""")
        agg = {}
        for r in rows:
            a = agg.setdefault(r["label"], {"label": r["label"], "shipping": 0, "waiting": 0})
            if r["order_state"] == "SHIPPING":
                a["shipping"] = int(r["n"] or 0)
            else:
                a["waiting"] = int(r["n"] or 0)
        unshipped.extend(sorted(agg.values(), key=lambda x: -(x["shipping"] + x["waiting"])))

    def _todo_supplier_stale():
        # 供应商价格断更报警（2026-07-15断供两天靠导出闸门才发现——以后第一时间跳出来）
        for label, tbl in (("豪雅", "newestdropship"), ("司顺", "newestdropship_vevor")):
            r = q1(f"SELECT TIMESTAMPDIFF(HOUR, MAX(Updated_At), NOW()) AS h "
                   f"FROM autooperate.{tbl}")
            h = int(r["h"]) if r and r["h"] is not None else None
            if h is not None and h >= 24:
                todos.append({"icon": "🛑", "label": f"{label}供应商价格已{h}小时没更新（同步任务可能挂了）",
                              "count": None, "money": None,
                              "href": url_for("main.health"), "danger": True})

    def _returns_yesterday():
        rows = qall("""
            SELECT CONCAT(platform, '-', shop_name) AS label, COUNT(*) AS n
            FROM order_system.mirakl_returns
            WHERE DATE(date_created) = DATE_SUB(CURDATE(), INTERVAL 1 DAY)
            GROUP BY platform, shop_name ORDER BY n DESC""")
        returns_y["rows"] = [{"label": r["label"], "n": int(r["n"] or 0)} for r in rows]
        returns_y["total"] = sum(r["n"] for r in returns_y["rows"])

    # ---------- 未发货订单明细（常用功能下方，分页50/页，用户2026-07-18拍板） ----------
    # 未发货全量只有几百行：一次取出+全量富化，筛选(店铺/运营/供应商)和排序在内存做，
    # 灵活且SQL简单。若以后未发货涨到几千行再改回SQL分页。
    uo_shop = (request.args.get("uo_shop") or "").strip()
    uo_op = (request.args.get("uo_op") or "").strip()
    uo_sup = (request.args.get("uo_sup") or "").strip()
    uo_sort = (request.args.get("uo_sort") or "").strip()
    try:
        uo_page = max(1, int(request.args.get("uo_page") or 1))
    except (TypeError, ValueError):
        uo_page = 1
    UO_PER = 50
    uo = {"rows": [], "total": 0, "page": uo_page, "pages": 1,
          "shops": [], "shop": uo_shop, "per": UO_PER,
          "op": uo_op, "sup": uo_sup, "sort": uo_sort, "amount": 0.0}

    _UO_OPS = {"刘梦蝶": ("MDLW", "MD"), "明瑞瑞": ("MRLW", "MR"), "朱以超": ("YCLW", "YC")}

    def _uo_operator(sku):
        s = (sku or "").upper()
        for name, (seg, head) in _UO_OPS.items():
            if seg in s or s.startswith(head):
                return name
        return "未分配"

    def _unshipped_detail():
        # 邮箱列 Lowes 是空的，真身在 raw_json 的 customer.customer_id 里——JSON兜底取；
        # 电话也一并抽出来，可疑买家匹配走 邮箱+电话 两条线
        union = " UNION ALL ".join(
            f"""SELECT sc.platform, sc.shop_name, d.order_id, d.order_line_id,
                       d.created_date, d.offer_sku, d.product_title, d.quantity,
                       d.line_total_price, d.order_state,
                       COALESCE(NULLIF(d.customer_email,''),
                         CASE WHEN d.raw_json IS NOT NULL AND JSON_VALID(d.raw_json)
                              THEN JSON_UNQUOTE(JSON_EXTRACT(d.raw_json,
                                   '$.customer.customer_id')) END) AS customer_email,
                       CASE WHEN d.raw_json IS NOT NULL AND JSON_VALID(d.raw_json)
                            THEN JSON_UNQUOTE(JSON_EXTRACT(d.raw_json,
                                 '$.customer.shipping_address.phone')) END AS customer_phone
                FROM order_system.{t} d
                JOIN order_system.shop_configs sc ON sc.id=d.shop_id
                WHERE d.order_state IN ('SHIPPING','WAITING_ACCEPTANCE')"""
            for t in ("macy_order_data", "lowes_order_data", "bestbuy_order_data"))
        rows = qall(f"SELECT * FROM ({union}) u")
        # HD订单（Teapplix同步，2026-07-18接入）：并入未发货明细。
        # HD没有"未接单"概念统一算待发货；45天前的未发货老单是Teapplix侧僵尸，不展示
        for h in qall("""
                SELECT store_key, invoice, txn_id, line_number, payment_date,
                       item_sku, item_desc, quantity, amount, warehouse_sku,
                       buyer_name, phone
                FROM order_system.hd_order_data
                WHERE shipped=0
                  AND payment_date >= DATE_SUB(CURDATE(), INTERVAL 45 DAY)"""):
            rows.append({
                "platform": f"HD-{h['store_key']}", "shop_name": h["store_key"],
                "order_id": h["invoice"] or h["txn_id"],
                "order_line_id": str(h["line_number"]),
                "created_date": h["payment_date"],
                "offer_sku": h["item_sku"], "product_title": h["item_desc"],
                "quantity": h["quantity"], "line_total_price": h["amount"],
                "order_state": "SHIPPING",
                "customer_email": "", "customer_phone": h["phone"],
                "hd_wh": h["warehouse_sku"],
            })
        uo["shops"] = sorted({f"{r['platform']}|{r['shop_name']}" for r in rows})
        uo["shops"] = [{"key": k, "label": k.split("|", 1)[0]} for k in uo["shops"]]
        if not rows:
            return
        # 全量富化（未发货SKU数很少）：产品图/供应商(缓存)、供应商库存、本店30/90天出单
        skus = list({r["offer_sku"] for r in rows if r["offer_sku"]})
        ph = ",".join(["%s"] * len(skus))
        wh_map = {r["shop_sku"]: r["warehouse_sku"] for r in qall(
            f"""SELECT DISTINCT shop_sku, warehouse_sku
                FROM order_system.offerprice_listing
                WHERE shop_sku IN ({ph}) AND warehouse_sku IS NOT NULL
                  AND warehouse_sku<>''""", skus)}
        whs = list({w for w in wh_map.values()}
                   | {r["hd_wh"] for r in rows if r.get("hd_wh")})
        cache, stock = {}, {}
        if whs:
            wph = ",".join(["%s"] * len(whs))
            for r in qall(f"""SELECT sku, MAX(supplier) AS supplier, MAX(image_url) AS img
                              FROM order_system.safety_product_cache
                              WHERE sku IN ({wph}) GROUP BY sku""", whs):
                cache[r["sku"]] = r
            for tbl in ("newestdropship", "newestdropship_vevor"):
                try:
                    for r in qall(f"SELECT SKU, Stock FROM autooperate.{tbl} "
                                  f"WHERE SKU IN ({wph})", whs):
                        stock.setdefault(r["SKU"], r["Stock"])
                except Exception:
                    pass
        sales = {}
        for t in ("macy_order_data", "lowes_order_data", "bestbuy_order_data"):
            for r in qall(f"""
                SELECT sc.platform, sc.shop_name, d.offer_sku,
                       COUNT(DISTINCT CASE WHEN d.created_date >=
                             DATE_SUB(CURDATE(), INTERVAL 30 DAY)
                             THEN d.order_id END) AS n30,
                       COUNT(DISTINCT d.order_id) AS n90,
                       COUNT(DISTINCT CASE WHEN mr.order_id IS NOT NULL
                             THEN d.order_id END) AS ret90
                FROM order_system.{t} d
                JOIN order_system.shop_configs sc ON sc.id=d.shop_id
                LEFT JOIN order_system.mirakl_returns mr ON mr.order_id=d.order_id
                WHERE d.offer_sku IN ({ph}) AND d.order_state<>'CANCELED'
                  AND d.created_date >= DATE_SUB(CURDATE(), INTERVAL 90 DAY)
                GROUP BY sc.platform, sc.shop_name, d.offer_sku""", skus):
                sales[(r["platform"], r["shop_name"], r["offer_sku"])] = (
                    int(r["n30"] or 0), int(r["n90"] or 0), int(r["ret90"] or 0))
        # HD的30/90天出单（退货数据未接，ret按0记、页面显示为—）
        hd_skus = list({r["offer_sku"] for r in rows
                        if str(r["platform"]).startswith("HD-") and r["offer_sku"]})
        if hd_skus:
            hph = ",".join(["%s"] * len(hd_skus))
            for r in qall(f"""
                SELECT store_key, item_sku,
                       COUNT(DISTINCT CASE WHEN payment_date >=
                             DATE_SUB(CURDATE(), INTERVAL 30 DAY)
                             THEN txn_id END) AS n30,
                       COUNT(DISTINCT txn_id) AS n90
                FROM order_system.hd_order_data
                WHERE item_sku IN ({hph})
                  AND payment_date >= DATE_SUB(CURDATE(), INTERVAL 90 DAY)
                GROUP BY store_key, item_sku""", hd_skus):
                sales[(f"HD-{r['store_key']}", r["store_key"], r["item_sku"])] = (
                    int(r["n30"] or 0), int(r["n90"] or 0), 0)
        # 待发单：未发货集合里该SKU压了几单（同款打包处理/爆款信号，用户2026-07-18定）
        pending_cnt = {}
        for r in rows:
            k = (r["platform"], r["shop_name"], r["offer_sku"])
            pending_cnt[k] = pending_cnt.get(k, 0) + 1
        # 可疑买家标记：邮箱/电话任一命中 黑名单 / 高中可疑档案（发货前最后一道闸）
        import re as _re

        def _digits(p):
            d = _re.sub(r"\D", "", p or "")
            return d[-10:] if len(d) >= 10 else (d if len(d) >= 7 else "")

        bl_emails, bl_phones = set(), set()
        for b in qall("""SELECT email, phone FROM order_system.customer_blacklist
                         WHERE active=1"""):
            e = (b["email"] or "").strip().lower()
            if "@" in e:
                bl_emails.add(e)
            p_ = _digits(b["phone"])
            if p_:
                bl_phones.add(p_)
        risk_emails, risk_phones = set(), set()
        for r_ in qall("""SELECT id_type, id_norm FROM order_system.customer_risk_profile
                          WHERE id_type IN ('email','phone')
                            AND risk_level IN ('high','mid')"""):
            (risk_emails if r_["id_type"] == "email" else risk_phones).add(r_["id_norm"])
        for r in rows:
            wh = r.get("hd_wh") or wh_map.get(r["offer_sku"])
            c = cache.get(wh) or {}
            r["img"] = c.get("img")
            r["supplier"] = c.get("supplier") or ""
            r["stock"] = stock.get(wh)
            r["n30"], r["n90"], ret90 = sales.get(
                (r["platform"], r["shop_name"], r["offer_sku"]), (0, 0, 0))
            r["ret_rate"] = (None if str(r["platform"]).startswith("HD-")
                             else (ret90 / r["n90"]) if r["n90"] else None)
            r["n_pending"] = pending_cnt.get(
                (r["platform"], r["shop_name"], r["offer_sku"]), 1)
            r["operator"] = _uo_operator(r["offer_sku"])
            em = (r.get("customer_email") or "").strip().lower()
            if "@" not in em:
                em = ""
            pn = _digits(r.get("customer_phone"))
            r["buyer_flag"] = (
                "black" if (em and em in bl_emails) or (pn and pn in bl_phones)
                else "risk" if (em and em in risk_emails) or (pn and pn in risk_phones)
                else None)

        # 无货压单统计（全量、不受筛选影响）→ 首页红条
        def _f2(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0
        zero = [r for r in rows
                if r["stock"] is not None and int(r["stock"] or 0) == 0]
        uo["zero_stock_n"] = len(zero)
        uo["zero_stock_amt"] = round(sum(_f2(r["line_total_price"]) for r in zero), 2)

        # 筛选（内存）
        if uo_shop and "|" in uo_shop:
            pf, sn = uo_shop.split("|", 1)
            rows = [r for r in rows if r["platform"] == pf and r["shop_name"] == sn]
        if uo_op:
            rows = [r for r in rows if r["operator"] == uo_op]
        if uo_sup:
            rows = [r for r in rows if r["supplier"] == uo_sup]

        # 排序（内存）
        def _f(v, d=0.0):
            try:
                return float(v)
            except (TypeError, ValueError):
                return d
        sorters = {
            "time_desc": lambda r: (r["created_date"] is None, ),
            "time_asc": None,   # 特判
            "amount_desc": lambda r: -_f(r["line_total_price"]),
            "stock_asc": lambda r: (r["stock"] is None, _f(r["stock"], 9e9)),
            "pending_desc": lambda r: -r["n_pending"],
            "n30_desc": lambda r: -r["n30"],
            "n90_desc": lambda r: -r["n90"],
            "ret_desc": lambda r: (r["ret_rate"] is None, -(r["ret_rate"] or 0), -r["n90"]),
        }
        if uo_sort == "time_desc":
            rows.sort(key=lambda r: r["created_date"] or datetime(1970, 1, 1), reverse=True)
        elif uo_sort == "time_asc":
            rows.sort(key=lambda r: r["created_date"] or datetime(2999, 1, 1))
        elif uo_sort in sorters and sorters[uo_sort]:
            rows.sort(key=sorters[uo_sort])
        else:   # 默认：未接单优先，再按时间新→旧
            rows.sort(key=lambda r: (0 if r["order_state"] == "WAITING_ACCEPTANCE" else 1,
                                     -(r["created_date"] or datetime(1970, 1, 1)).timestamp()))

        # 分页（内存切片）；总金额跟随当前筛选
        uo["amount"] = round(sum(_f(r["line_total_price"]) for r in rows), 2)
        uo["total"] = len(rows)
        uo["pages"] = max(1, (uo["total"] + UO_PER - 1) // UO_PER)
        uo["page"] = min(uo_page, uo["pages"])
        uo["rows"] = rows[(uo["page"] - 1) * UO_PER: uo["page"] * UO_PER]

    # ---------- 昨日退货明细（未发货明细下方，用户2026-07-18拍板） ----------
    ry_rows = []

    _REASON_CN = {
        "RETURN_CM_DONT_WANT": "不想要了", "RETURN_PRODUCT_DOES_NOT_FIT": "尺寸不合适",
        "RETURN_CM_QUALITY": "质量问题", "RETURN_CM_DAMAGED": "损坏",
        "RETURN_CM_NOT_AS_DESCRIBED": "与描述不符", "RETURN_CM_DEFECTIVE": "有缺陷",
        "RETURN_CM_WRONG_ITEM": "发错货", "RETURN_CM_LATE": "到货太晚",
    }

    def _returns_detail():
        rets = qall("""
            SELECT platform, shop_name, order_id, reason_code, tracking_number,
                   date_created
            FROM order_system.mirakl_returns
            WHERE DATE(date_created) = DATE_SUB(CURDATE(), INTERVAL 1 DAY)
            ORDER BY date_created DESC LIMIT 200""")
        if not rets:
            return
        oids = list({r["order_id"] for r in rets if r["order_id"]})
        ph = ",".join(["%s"] * len(oids))
        # 订单信息（每单取一行）
        oinfo = {}
        for t in ("macy_order_data", "lowes_order_data", "bestbuy_order_data"):
            for r in qall(f"""
                SELECT d.order_id, d.offer_sku, d.product_title, d.line_total_price,
                       d.created_date
                FROM order_system.{t} d
                JOIN (SELECT MIN(id) AS mid FROM order_system.{t}
                      WHERE order_id IN ({ph}) GROUP BY order_id) f ON f.mid=d.id""",
                oids):
                oinfo.setdefault(r["order_id"], r)
        skus = list({o["offer_sku"] for o in oinfo.values() if o["offer_sku"]})
        wh_map, cache, sales = {}, {}, {}
        if skus:
            sph = ",".join(["%s"] * len(skus))
            wh_map = {r["shop_sku"]: r["warehouse_sku"] for r in qall(
                f"""SELECT DISTINCT shop_sku, warehouse_sku
                    FROM order_system.offerprice_listing
                    WHERE shop_sku IN ({sph}) AND warehouse_sku IS NOT NULL
                      AND warehouse_sku<>''""", skus)}
            whs = list({w for w in wh_map.values()})
            if whs:
                wph = ",".join(["%s"] * len(whs))
                for r in qall(f"""SELECT sku, MAX(supplier) AS supplier,
                                  MAX(image_url) AS img
                                  FROM order_system.safety_product_cache
                                  WHERE sku IN ({wph}) GROUP BY sku""", whs):
                    cache[r["sku"]] = r
            for t in ("macy_order_data", "lowes_order_data", "bestbuy_order_data"):
                for r in qall(f"""
                    SELECT sc.platform, sc.shop_name, d.offer_sku,
                           COUNT(DISTINCT CASE WHEN d.created_date >=
                                 DATE_SUB(CURDATE(), INTERVAL 30 DAY)
                                 THEN d.order_id END) AS n30,
                           COUNT(DISTINCT d.order_id) AS n90,
                           COUNT(DISTINCT CASE WHEN mr.order_id IS NOT NULL
                                 THEN d.order_id END) AS ret90
                    FROM order_system.{t} d
                    JOIN order_system.shop_configs sc ON sc.id=d.shop_id
                    LEFT JOIN order_system.mirakl_returns mr ON mr.order_id=d.order_id
                    WHERE d.offer_sku IN ({sph}) AND d.order_state<>'CANCELED'
                      AND d.created_date >= DATE_SUB(CURDATE(), INTERVAL 90 DAY)
                    GROUP BY sc.platform, sc.shop_name, d.offer_sku""", skus):
                    sales[(r["platform"], r["shop_name"], r["offer_sku"])] = (
                        int(r["n30"] or 0), int(r["n90"] or 0), int(r["ret90"] or 0))
        for r in rets:
            o = oinfo.get(r["order_id"]) or {}
            sku = o.get("offer_sku") or ""
            wh = wh_map.get(sku)
            c = cache.get(wh) or {}
            n30, n90, ret90 = sales.get((r["platform"], r["shop_name"], sku), (0, 0, 0))
            ry_rows.append({
                "platform": r["platform"], "shop_name": r["shop_name"],
                "order_id": r["order_id"],
                "reason": _REASON_CN.get(r["reason_code"], r["reason_code"] or "—"),
                "reason_raw": r["reason_code"] or "",
                "tracking": (r["tracking_number"] or "").strip(),
                "ret_time": r["date_created"],
                "offer_sku": sku, "title": o.get("product_title"),
                "amount": o.get("line_total_price"),
                "order_date": o.get("created_date"),
                "img": c.get("img"), "supplier": c.get("supplier") or "",
                "operator": _uo_operator(sku),
                "n30": n30, "n90": n90,
                "ret_rate": (ret90 / n90) if n90 else None,
            })

    try:
        for fn in (_todo_unfiled, _todo_near_writeoff, _todo_sentinel, _todo_issues,
                   _todo_repricing, _todo_supplier_stale, _unshipped,
                   _returns_yesterday, _unshipped_detail, _returns_detail):
            _safe(fn)
    finally:
        conn.close()

    # 未发货×无货 红条（依赖 _unshipped_detail 的统计，所以放循环后补）
    if uo.get("zero_stock_n"):
        todos.append({"icon": "📦",
                      "label": "未发货但供应商无货（发不出会超时，优先处理）",
                      "count": uo["zero_stock_n"], "money": uo.get("zero_stock_amt"),
                      "href": url_for("main.index", uo_sort="stock_asc") + "#unshipped-detail",
                      "danger": True})

    return render_template('index.html', todos=todos, unshipped=unshipped,
                           returns_y=returns_y, uo=uo, ry_rows=ry_rows)


@main_bp.route('/feishu-dashboard')
def feishu_dashboard():
    """嵌入飞书多维表格仪表盘（实时数据）。链接存 instance/feishu_dashboard_url.txt，
    一行一个：标签|分享链接，改文件即生效不用重启。"""
    import os
    from flask import current_app
    boards = []
    path = os.path.join(current_app.instance_path, "feishu_dashboard_url.txt")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "|" in line:
                    label, url = line.split("|", 1)
                else:
                    label, url = "飞书仪表盘", line
                boards.append({"label": label.strip(), "url": url.strip()})
    except FileNotFoundError:
        pass
    return render_template('feishu_dashboard.html', boards=boards)


@main_bp.route('/health')
def health():
    conn = DBManager.get_connection()
    rows = []
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT store_key,
                       last_synced_at,
                       updated_at,
                       TIMESTAMPDIFF(MINUTE, updated_at, NOW()) AS minutes_stale
                FROM order_system.txn_sync_cursor
                ORDER BY store_key
                """
            )
            for r in cursor.fetchall():
                store = r['store_key']
                minutes_stale = r['minutes_stale'] or 0
                hours_stale = round(minutes_stale / 60, 1)
                threshold = STALE_THRESHOLD_HOURS.get(store, DEFAULT_STALE_HOURS)
                is_stale = minutes_stale > threshold * 60
                rows.append({
                    'store_key': store,
                    'last_synced_at': r['last_synced_at'],
                    'updated_at': r['updated_at'],
                    'hours_stale': hours_stale,
                    'threshold_hours': threshold,
                    'is_stale': is_stale,
                })
    finally:
        conn.close()

    overall_ok = all(not r['is_stale'] for r in rows)

    # Offer 全量/增量同步状态（offer_sync_cursor）
    offer_sync_rows = []
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT store_key, last_request_date, last_run_at,
                       last_tracking_id, last_offer_count, last_new_count,
                       last_status,
                       TIMESTAMPDIFF(MINUTE, last_run_at, NOW()) AS minutes_since
                FROM order_system.offer_sync_cursor
                ORDER BY store_key
                """
            )
            for r in cursor.fetchall():
                minutes_since = r['minutes_since'] or 0
                status = (r.get('last_status') or '').lower()
                # 成功标志：completed / completed_no_data 都算成功
                is_ok = status.startswith('completed')
                # 超过 30 小时没跑也标红（cron 是每天一次）
                is_stale = minutes_since > 30 * 60
                offer_sync_rows.append({
                    'store_key': r['store_key'],
                    'last_run_at': r['last_run_at'],
                    'hours_since': round(minutes_since / 60, 1),
                    'last_offer_count': r.get('last_offer_count') or 0,
                    'last_new_count': r.get('last_new_count') or 0,
                    'last_status': r.get('last_status') or '',
                    'is_ok': is_ok,
                    'is_stale': is_stale,
                })
    except Exception:
        # offer_sync_cursor 表可能还没建（首次同步前）
        offer_sync_rows = []
    finally:
        conn.close()

    return render_template(
        'health.html',
        rows=rows,
        overall_ok=overall_ok,
        offer_sync_rows=offer_sync_rows,
    )
