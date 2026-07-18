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
        union = " UNION ALL ".join(
            f"""SELECT sc.platform, sc.shop_name, d.order_id, d.order_line_id,
                       d.created_date, d.offer_sku, d.product_title, d.quantity,
                       d.line_total_price, d.order_state
                FROM order_system.{t} d
                JOIN order_system.shop_configs sc ON sc.id=d.shop_id
                WHERE d.order_state IN ('SHIPPING','WAITING_ACCEPTANCE')"""
            for t in ("macy_order_data", "lowes_order_data", "bestbuy_order_data"))
        rows = qall(f"SELECT * FROM ({union}) u")
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
        whs = list({w for w in wh_map.values()})
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
        # 待发单：未发货集合里该SKU压了几单（同款打包处理/爆款信号，用户2026-07-18定）
        pending_cnt = {}
        for r in rows:
            k = (r["platform"], r["shop_name"], r["offer_sku"])
            pending_cnt[k] = pending_cnt.get(k, 0) + 1
        for r in rows:
            wh = wh_map.get(r["offer_sku"])
            c = cache.get(wh) or {}
            r["img"] = c.get("img")
            r["supplier"] = c.get("supplier") or ""
            r["stock"] = stock.get(wh)
            r["n30"], r["n90"], ret90 = sales.get(
                (r["platform"], r["shop_name"], r["offer_sku"]), (0, 0, 0))
            r["ret_rate"] = (ret90 / r["n90"]) if r["n90"] else None
            r["n_pending"] = pending_cnt.get(
                (r["platform"], r["shop_name"], r["offer_sku"]), 1)
            r["operator"] = _uo_operator(r["offer_sku"])

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

    try:
        for fn in (_todo_unfiled, _todo_near_writeoff, _todo_sentinel, _todo_issues,
                   _todo_repricing, _unshipped, _returns_yesterday, _unshipped_detail):
            _safe(fn)
    finally:
        conn.close()

    return render_template('index.html', todos=todos, unshipped=unshipped,
                           returns_y=returns_y, uo=uo)


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
