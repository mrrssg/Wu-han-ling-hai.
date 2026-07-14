# -*- coding: utf-8 -*-
"""
利润控制台 Phase 1 数据层（只读现有数据源，只写本模块4张表）。

每日聚合 job（scripts/profit_control_daily.py 调 run_daily_aggregation()）：
  1. 拉飞书订单表（财务口径，实际优先）
  2. 拉 mirakl_returns 真实退货日
  3. 重建 return_case 退货三态台账（近365天，UPSERT）
  4. 写 profit_cell_daily 当日快照（运营×店铺，链梯法成熟度修正）
  5. 跑问题规则引擎 → issue_log

口径约定（与 sku_panel_auto.py 对齐）：
  - 售价 = 整单总价（不乘数量）
  - 利润：实际数据完整用实际，否则预估
  - is_return = 「预估退货运费」有值 且 「退货原因」非空
  - 排除：取消单 / BestBuy-Delphi / Walmart / 供应商为空
  - 退货运费：实际「退货运费」优先；无实际时 Lowes 店=0（用户规则：Lowes买家退实体店），
    其它店=「预估退货运费」
  - 供应商退款：只认「供应商退款」字段的实际回填值——政策不写死，逐笔看数据（用户规则）
  - 三态：refund>0 → recovered；无退款且账龄>WRITEOFF_DAYS → written_off；否则 pending
"""
import json
import math
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from app.models.db_manager import DBManager

# ---- 可调参数（Phase 1 先做成模块常量，后续可挪到配置表） ----
BASELINE_MARGIN = 0.10          # 考核基线：cell 修正利润率地板线
WRITEOFF_DAYS = 180             # 待追回超过N天视为收不回（confirmed loss）
MATURED_AGE_DAYS = 90           # 计算回收率时，账龄超过N天的 case 视为"已到期"
ULTIMATE_COHORT_AGE = 120       # 计算最终退货率的成熟 cohort 最小账龄
CELL_MIN_SALE_FOR_ISSUE = 1000  # 销售额低于此值的 cell 不报"破线"问题（噪音）
NEG_EV_SKU_THRESHOLD = -50      # SKU 净贡献低于此值报"负期望SKU"
RECOVERY_OVERDUE_DAYS = 60      # 待追回账龄超过N天进"追款超期"问题
BACKFILL_STALE_DAYS = 45        # 实际成本回填滞后阈值（数据新鲜度规则）

EXCLUDE_STORES = {"BestBuy-Delphi", "Walmart"}
LOWES_STORE_PREFIX = "Lowes"

FEISHU_APP_ID = "cli_a940a2a1067adbd2"
FEISHU_APP_SECRET = "i2mKLGVzUDmu4v0U9HYEYdMGc0ZvZAgU"
ORDER_APP_TOKEN = "WKeRbmf7ra9nJZs77smc2AA2nAg"
ORDER_TABLE_ID = "tbl4LDs0H5M8Pq5n"
# 退货登记表（财务/客服追款登记）：登记过「订单」的=已向供应商追过款，只等退款，
# 不再进追款清单（用户规则 2026-07-14）
CLAIM_TABLE_ID = "tblCqER404qe57vV"

PAGE_SIZE = 500
REQUEST_TIMEOUT = 60
PAGE_DELAY_SECONDS = 0.25

CN_TZ = timezone(timedelta(hours=8))


# =====================================================================
# 飞书订单表拉取
# =====================================================================

def _feishu_token() -> str:
    r = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"feishu auth failed: {data}")
    return data["tenant_access_token"]


def _gn(v) -> Optional[float]:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        val = v.get("value")
        if isinstance(val, list) and val:
            x = val[0]
            return float(x) if isinstance(x, (int, float)) else None
        return None
    return None


def _gt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, list) and v and isinstance(v[0], dict):
        return v[0].get("text") or v[0].get("name") or ""
    return str(v)


def fetch_feishu_orders() -> List[Dict[str, Any]]:
    """整表拉取飞书订单表（36k+ 行，~75 页）。抗限流重试。"""
    token = _feishu_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{ORDER_APP_TOKEN}"
           f"/tables/{ORDER_TABLE_ID}/records/search?page_size={PAGE_SIZE}")
    items: List[Dict] = []
    page_token = None
    while True:
        u = url + (f"&page_token={page_token}" if page_token else "")
        body = {"automatic_fields": False}
        data = None
        for attempt in range(5):
            try:
                resp = requests.post(u, headers=headers,
                                     data=json.dumps(body).encode("utf-8"),
                                     timeout=REQUEST_TIMEOUT)
                data = resp.json()
                if data.get("data") is not None:
                    break
            except Exception:
                data = None
            time.sleep(1.5 * (attempt + 1))
        if not data or data.get("data") is None:
            raise RuntimeError(f"feishu order fetch failed after retries: {data}")
        items += data["data"].get("items") or []
        page_token = data["data"].get("page_token")
        if not page_token:
            break
        time.sleep(PAGE_DELAY_SECONDS)
    return items


def fetch_claim_filed_orders() -> set:
    """拉退货登记表的「订单」列 → 已追过款的订单号集合。
    归一化：去空白；'4652515820-A-1' 这类拆包后缀同时收录基号 '4652515820-A'。"""
    token = _feishu_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{ORDER_APP_TOKEN}"
           f"/tables/{CLAIM_TABLE_ID}/records/search?page_size={PAGE_SIZE}")
    orders: set = set()
    page_token = None
    while True:
        u = url + (f"&page_token={page_token}" if page_token else "")
        body = {"field_names": ["订单"], "automatic_fields": False}
        data = None
        for attempt in range(5):
            try:
                resp = requests.post(u, headers=headers,
                                     data=json.dumps(body).encode("utf-8"),
                                     timeout=REQUEST_TIMEOUT)
                data = resp.json()
                if data.get("data") is not None:
                    break
            except Exception:
                data = None
            time.sleep(1.5 * (attempt + 1))
        if not data or data.get("data") is None:
            raise RuntimeError(f"feishu claim table fetch failed: {data}")
        for it in data["data"].get("items") or []:
            o = _gt((it.get("fields") or {}).get("订单")).strip()
            if not o:
                continue
            orders.add(o)
            m = re.match(r"^(.+-[A-Z])-\d+$", o)
            if m:
                orders.add(m.group(1))
        page_token = data["data"].get("page_token")
        if not page_token:
            break
        time.sleep(PAGE_DELAY_SECONDS)
    return orders


def sync_claim_filed(conn) -> int:
    """return_case.claim_filed 标记同步：在退货登记表登记过的订单=已追过款。
    每日重建后调用（rebuild 的 UPSERT 不含该列，重跑不丢，此处全量重刷保证登记撤销也能回退）。"""
    claimed = fetch_claim_filed_orders()
    with conn.cursor() as cur:
        cur.execute("UPDATE order_system.return_case SET claim_filed=0 WHERE claim_filed=1")
        flagged = 0
        ids = sorted(claimed)
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            ph = ",".join(["%s"] * len(chunk))
            cur.execute(f"UPDATE order_system.return_case SET claim_filed=1 "
                        f"WHERE order_id IN ({ph})", chunk)
            flagged += cur.rowcount
    conn.commit()
    return flagged


# =====================================================================
# 订单解析（口径与 sku_panel_auto.py 对齐）
# =====================================================================

def parse_orders(raw_items: List[Dict]) -> List[Dict[str, Any]]:
    parsed = []
    for r in raw_items:
        f = r.get("fields") or {}
        store = _gt(f.get("店铺"))
        if not store or store in EXCLUDE_STORES:
            continue
        status = _gt(f.get("订单状态"))
        if status and status.upper() in ("CANCELLED", "CANCELED"):
            continue
        supplier = _gt(f.get("供应商"))
        if not supplier:
            continue
        t = _gn(f.get("下单时间")) or 0
        if t <= 0:
            continue

        cost_a = _gn(f.get("实际成本"))
        cost_e = _gn(f.get("成本"))
        inc_a = _gn(f.get("实际到账"))
        inc_e = _gn(f.get("预估到账"))
        pr_a = _gn(f.get("实际利润"))
        pr_e = _gn(f.get("预估利润"))
        sup_refund_actual = _gn(f.get("供应商退款"))

        # 实际数据残缺 → 走预估（两种已知残缺形态，与决策系统同款）
        incomplete_actual = (
            (inc_a == 0 and (sup_refund_actual is None or sup_refund_actual == 0)
             and pr_a is not None and pr_e is not None and pr_a < pr_e)
            or (cost_a is None or cost_a == 0)
        )
        use_actual = (inc_a is not None) and not incomplete_actual
        cost = cost_a if use_actual else (cost_e or 0)
        profit = (pr_a if pr_a is not None else 0) if use_actual else (pr_e if pr_e is not None else 0)

        est_rfee = _gn(f.get("预估退货运费"))
        reason = _gt(f.get("退货原因"))
        # 退货判定(用户规则 2026-07-13)：标记(预估退货运费+退货原因)只代表"申请过退货"，
        # 真退货必须账单坐实——实际到账≈0(买家货款被扣回)才算；
        # 账单未导入(实际到账为空)暂按退货预判；账单到了但没扣款(>1)的按正常单算利润。
        return_marked = (est_rfee is not None) and reason != ""
        is_return = return_marked and (inc_a is None or inc_a <= 1)

        rfee = 0.0
        if return_marked:
            rfee_a = _gn(f.get("退货运费"))
            if use_actual and rfee_a is not None:
                rfee = rfee_a
            elif store.startswith(LOWES_STORE_PREFIX):
                rfee = 0.0    # 用户规则：Lowes 退实体店，卖家不出运费
            else:
                rfee = est_rfee or 0.0

        parsed.append({
            "order_id": _gt(f.get("订单号")),
            "order_line": _gt(f.get("订单行号")) or "1",
            "store": store,
            "supplier": supplier,
            "operator": _gt(f.get("责任运营")) or "未分配",
            "sku": _gt(f.get("Shop SKU")) or _gt(f.get("供应商sku")),
            "order_ts": t,
            "income_actual": inc_a,
            "profit_actual_raw": pr_a,
            "sale": _gn(f.get("售价")) or 0.0,
            "cost": cost or 0.0,
            "profit": profit or 0.0,
            "is_return": is_return,
            "return_marked": return_marked,
            "use_actual": use_actual,
            "return_fee": rfee,
            "supplier_refund": sup_refund_actual,
            "actual_cost_filled": (cost_a is not None and cost_a != 0),
        })
    return parsed


# =====================================================================
# mirakl_returns 真实退货日
# =====================================================================

def _bare(order_id: str) -> str:
    s = str(order_id or "")
    out = []
    for ch in s:
        if ch.isdigit():
            out.append(ch)
        else:
            break
    return "".join(out) if out else s


def load_return_dates(conn) -> Dict[str, datetime]:
    dates: Dict[str, datetime] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT order_id AS oid, MIN(date_created) AS d FROM order_system.mirakl_returns "
            "WHERE order_id IS NOT NULL AND order_id<>'' GROUP BY order_id")
        for row in cur.fetchall():
            oid = row["oid"] if isinstance(row, dict) else row[0]
            d = row["d"] if isinstance(row, dict) else row[1]
            if oid and d:
                dates[_bare(oid)] = d
    return dates


# =====================================================================
# return_case 三态台账
# =====================================================================

def rebuild_return_cases(conn, parsed: List[Dict], return_dates: Dict[str, datetime],
                         now_cn: datetime) -> Dict[str, Any]:
    """近365天退货单 → return_case UPSERT。返回统计摘要。"""
    cutoff_ts = (now_cn - timedelta(days=365)).timestamp() * 1000
    rows = []
    for o in parsed:
        if not o["return_marked"] or o["order_ts"] <= cutoff_ts:
            continue
        order_date = datetime.fromtimestamp(o["order_ts"] / 1000, tz=CN_TZ).date()
        rd = return_dates.get(_bare(o["order_id"]))
        return_date = rd.date() if rd else order_date  # 查不到退货日 fallback 下单日
        age_days = (now_cn.date() - return_date).days
        refund = o["supplier_refund"]
        cost = round(o["cost"], 2)
        fee = round(o["return_fee"], 2)
        if not o["is_return"]:
            # 账单到了但买家货款没被扣回 → 不算退货，按正常单计利润（不计任何退货损失）
            state = "not_charged"
            confirmed_loss = 0.0
            exposure = 0.0
        elif refund is not None and refund > 0:
            state = "recovered"
            confirmed_loss = round(max(0.0, cost - refund) + fee, 2)
            exposure = 0.0
        elif age_days > WRITEOFF_DAYS:
            state = "written_off"
            confirmed_loss = round(cost + fee, 2)
            exposure = 0.0
        else:
            state = "pending"
            confirmed_loss = fee   # 运费是确定支出；货值是敞口
            exposure = cost
        rows.append((
            o["order_id"], o["order_line"], o["store"], o["operator"], o["supplier"],
            o["sku"], order_date, return_date, cost, fee,
            (round(refund, 2) if refund is not None else None),
            state, age_days, confirmed_loss, exposure,
            round(o["sale"], 2),
            (round(o["income_actual"], 2) if o["income_actual"] is not None else None),
            (round(o["profit_actual_raw"], 2) if o["profit_actual_raw"] is not None else None),
        ))

    sql = """
        INSERT INTO order_system.return_case
            (order_id, order_line, store, operator, supplier, shop_sku,
             order_date, return_date, cost, return_fee, supplier_refund,
             state, age_days, confirmed_loss, exposure, sale, income_actual, profit_actual)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            store=VALUES(store), operator=VALUES(operator), supplier=VALUES(supplier),
            shop_sku=VALUES(shop_sku), return_date=VALUES(return_date),
            cost=VALUES(cost), return_fee=VALUES(return_fee),
            supplier_refund=VALUES(supplier_refund),
            state=VALUES(state), age_days=VALUES(age_days),
            confirmed_loss=VALUES(confirmed_loss), exposure=VALUES(exposure),
            sale=VALUES(sale), income_actual=VALUES(income_actual),
            profit_actual=VALUES(profit_actual)
    """
    with conn.cursor() as cur:
        for i in range(0, len(rows), 500):
            cur.executemany(sql, rows[i:i + 500])
    conn.commit()
    by_state = defaultdict(int)
    for r in rows:
        by_state[r[11]] += 1
    return {"cases": len(rows), "by_state": dict(by_state)}


def compute_recovery_rates(conn) -> Dict[Tuple[str, str], float]:
    """按 (store, supplier) 的金额口径回收率：Σ退款/Σ成本，只统计已到期 case
    （recovered 或 账龄>MATURED_AGE_DAYS）。样本太小回退 store 级，再回退 0。"""
    rates: Dict[Tuple[str, str], float] = {}
    store_agg: Dict[str, List[float]] = defaultdict(lambda: [0.0, 0.0])
    with conn.cursor() as cur:
        cur.execute("""
            SELECT store, supplier,
                   SUM(COALESCE(supplier_refund,0)) AS refund_sum,
                   SUM(cost) AS cost_sum, COUNT(*) AS n
            FROM order_system.return_case
            WHERE state <> 'not_charged' AND (state='recovered' OR age_days > %s)
            GROUP BY store, supplier
        """, (MATURED_AGE_DAYS,))
        for row in cur.fetchall():
            store, sup = row["store"], row["supplier"]
            refund_sum = float(row["refund_sum"] or 0)
            cost_sum = float(row["cost_sum"] or 0)
            n = int(row["n"] or 0)
            store_agg[store][0] += refund_sum
            store_agg[store][1] += cost_sum
            if n >= 10 and cost_sum > 0:
                rates[(store, sup)] = min(1.0, refund_sum / cost_sum)
    store_rates = {s: (min(1.0, a[0] / a[1]) if a[1] > 0 else 0.0)
                   for s, a in store_agg.items()}
    rates["__store__"] = store_rates  # type: ignore
    return rates


# 用户规则(2026-07-13)：Macy店铺的Costway退货回收率≥90%；实测(86.5%)因财务回填滞后
# 偏低，故取 max(实测, 0.90)。实测超过90%后自动跟随实测。
MACY_COSTWAY_RECOVERY_FLOOR = 0.90


def _recovery_rate(rates, store: str, supplier: str) -> float:
    # 用户规则：司顺(Vevor)不管哪个店铺都不退货值
    s = str(supplier or "").strip().lower()
    if s.startswith("vevor") or s == "司顺":
        return 0.0
    r = rates.get((store, supplier))
    if str(store).startswith("Macys") and s == "costway":
        return max(r if r is not None else 0.0, MACY_COSTWAY_RECOVERY_FLOOR)
    if r is not None:
        return r
    return rates.get("__store__", {}).get(store, 0.0)


# =====================================================================
# 链梯法：退货成熟度曲线
# =====================================================================

def build_maturity_model(parsed: List[Dict], return_dates: Dict[str, datetime],
                         now_cn: datetime) -> Dict[str, Dict[str, Any]]:
    """按店铺：最终退货率 R∞（成熟 cohort）+ 退货滞后经验 CDF F(a)。"""
    lag_days_by_store: Dict[str, List[int]] = defaultdict(list)
    matured_orders: Dict[str, int] = defaultdict(int)
    matured_returns: Dict[str, int] = defaultdict(int)
    now_date = now_cn.date()
    for o in parsed:
        order_date = datetime.fromtimestamp(o["order_ts"] / 1000, tz=CN_TZ).date()
        age = (now_date - order_date).days
        if o["is_return"]:
            rd = return_dates.get(_bare(o["order_id"]))
            if rd:
                lag = (rd.date() - order_date).days
                if 0 <= lag <= 365:
                    lag_days_by_store[o["store"]].append(lag)
        if age >= ULTIMATE_COHORT_AGE:
            matured_orders[o["store"]] += 1
            if o["is_return"]:
                matured_returns[o["store"]] += 1

    pooled_lags = [l for lags in lag_days_by_store.values() for l in lags]
    model: Dict[str, Dict[str, Any]] = {}
    for store in set(list(matured_orders.keys()) + list(lag_days_by_store.keys())):
        lags = lag_days_by_store.get(store) or []
        if len(lags) < 30:
            lags = pooled_lags  # 样本不足合并全店
        lags_sorted = sorted(lags)
        n_ord = matured_orders.get(store, 0)
        ultimate = (matured_returns.get(store, 0) / n_ord) if n_ord >= 100 else None
        model[store] = {"lags_sorted": lags_sorted, "ultimate_rate": ultimate}
    return model


def _maturity_F(lags_sorted: List[int], age_days: int) -> float:
    """经验 CDF：到 age_days 为止预计已发生的退货占最终退货的比例。"""
    if not lags_sorted:
        return 1.0
    import bisect
    return bisect.bisect_right(lags_sorted, age_days) / len(lags_sorted)


# =====================================================================
# cell 快照
# =====================================================================

def build_cell_snapshots(conn, parsed: List[Dict], rates, maturity,
                         now_cn: datetime) -> List[Dict[str, Any]]:
    now_date = now_cn.date()
    cut30 = (now_cn - timedelta(days=30)).timestamp() * 1000
    cut90 = (now_cn - timedelta(days=90)).timestamp() * 1000

    cells: Dict[Tuple[str, str], Dict[str, Any]] = defaultdict(lambda: {
        "orders_30d": 0, "sale_30d": 0.0, "profit_30d": 0.0,
        "orders_90d": 0, "sale_90d": 0.0, "profit_all_90d": 0.0,
        "profit_gross_90d": 0.0, "returns_90d": 0,
        "confirmed_loss": 0.0, "pending_exposure": 0.0, "expected_pending_loss": 0.0,
        "future_loss": 0.0,
    })

    for o in parsed:
        if o["order_ts"] <= cut90:
            continue
        key = (o["operator"], o["store"])
        c = cells[key]
        c["orders_90d"] += 1
        c["sale_90d"] += o["sale"]
        c["profit_all_90d"] += o["profit"]
        if not o["is_return"]:
            c["profit_gross_90d"] += o["profit"]
        else:
            c["returns_90d"] += 1
        if o["order_ts"] > cut30:
            c["orders_30d"] += 1
            c["sale_30d"] += o["sale"]
            c["profit_30d"] += o["profit"]

        # 链梯：这单还没退，但按成熟度可能还会退 → 预扣期望损失
        m = maturity.get(o["store"]) or {}
        ultimate = m.get("ultimate_rate")
        if ultimate and not o["is_return"]:
            order_date = datetime.fromtimestamp(o["order_ts"] / 1000, tz=CN_TZ).date()
            age = (now_date - order_date).days
            p_future = ultimate * (1.0 - _maturity_F(m.get("lags_sorted") or [], age))
            if p_future > 0:
                rr = _recovery_rate(rates, o["store"], o["supplier"])
                fee_est = 0.0 if o["store"].startswith(LOWES_STORE_PREFIX) else o["cost"] * 0.10
                c["future_loss"] += p_future * (o["cost"] * (1.0 - rr) + fee_est)

    # 已发生退货的 case 损失（按下单日 90 天窗口对齐 cell）
    with conn.cursor() as cur:
        cur.execute("""
            SELECT operator, store, supplier, state,
                   SUM(confirmed_loss) AS closs, SUM(exposure) AS expo
            FROM order_system.return_case
            WHERE order_date > %s
            GROUP BY operator, store, supplier, state
        """, ((now_cn - timedelta(days=90)).date(),))
        for row in cur.fetchall():
            key = (row["operator"], row["store"])
            if key not in cells:
                continue
            c = cells[key]
            c["confirmed_loss"] += float(row["closs"] or 0)
            expo = float(row["expo"] or 0)
            if row["state"] == "pending" and expo > 0:
                rr = _recovery_rate(rates, row["store"], row["supplier"])
                c["pending_exposure"] += expo
                c["expected_pending_loss"] += expo * (1.0 - rr)

    snapshots = []
    for (operator, store), c in cells.items():
        sale90 = c["sale_90d"]
        margin_raw = (c["profit_all_90d"] / sale90) if sale90 > 0 else None
        total_loss = c["confirmed_loss"] + c["expected_pending_loss"] + c["future_loss"]
        margin_adj = ((c["profit_gross_90d"] - total_loss) / sale90) if sale90 > 0 else None
        gap = max(0.0, sale90 * BASELINE_MARGIN - (c["profit_gross_90d"] - total_loss)) if sale90 > 0 else 0.0
        m = maturity.get(store) or {}
        snapshots.append({
            "snapshot_date": now_date, "operator": operator, "store": store,
            "orders_30d": c["orders_30d"], "sale_30d": round(c["sale_30d"], 2),
            "profit_30d": round(c["profit_30d"], 2),
            "margin_30d": round(c["profit_30d"] / c["sale_30d"], 4) if c["sale_30d"] > 0 else None,
            "orders_90d": c["orders_90d"], "sale_90d": round(sale90, 2),
            "profit_gross_90d": round(c["profit_gross_90d"], 2),
            "margin_90d_raw": round(margin_raw, 4) if margin_raw is not None else None,
            "confirmed_return_loss_90d": round(c["confirmed_loss"], 2),
            "pending_exposure_90d": round(c["pending_exposure"], 2),
            "expected_pending_loss_90d": round(c["expected_pending_loss"], 2),
            "expected_future_loss_90d": round(c["future_loss"], 2),
            "margin_90d_adj": round(margin_adj, 4) if margin_adj is not None else None,
            "recovery_rate": round(_recovery_rate(rates, store, "Costway"), 4),
            "returns_90d": c["returns_90d"],
            "return_rate_90d": round(c["returns_90d"] / c["orders_90d"], 4) if c["orders_90d"] else None,
            "ultimate_return_rate": round(m["ultimate_rate"], 4) if m.get("ultimate_rate") else None,
            "baseline": BASELINE_MARGIN,
            "gap_usd": round(gap, 2),
            "meets_baseline": 1 if (margin_adj is not None and margin_adj >= BASELINE_MARGIN) else 0,
        })

    cols = list(snapshots[0].keys()) if snapshots else []
    if snapshots:
        placeholders = ",".join(["%s"] * len(cols))
        updates = ",".join([f"{c}=VALUES({c})" for c in cols if c not in
                            ("snapshot_date", "operator", "store")])
        sql = (f"INSERT INTO order_system.profit_cell_daily ({','.join(cols)}) "
               f"VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {updates}")
        with conn.cursor() as cur:
            cur.executemany(sql, [tuple(s[c] for c in cols) for s in snapshots])
        conn.commit()
    return snapshots


# =====================================================================
# 每日趋势序列（滚动30天修正净利率 + 每日净盈利，回溯重建，幂等）
# =====================================================================

def _order_expected_loss(o, rates) -> float:
    """一笔退货单的期望损失（当前认知口径）。非退货单返回0。"""
    if not o["is_return"]:
        return 0.0
    refund = o["supplier_refund"]
    if refund is not None and refund > 0:
        return max(0.0, o["cost"] - refund) + o["return_fee"]
    rr = _recovery_rate(rates, o["store"], o["supplier"])
    return o["cost"] * (1.0 - rr) + o["return_fee"]


def build_trend_daily(conn, parsed: List[Dict], rates, now_cn: datetime,
                      days: int = 120) -> int:
    """按下单日回溯构建 公司/各运营 的每日净贡献与滚动30天修正净利率。
    净贡献 = 当日非退货单毛利 − 当日(下单口径)退货单期望损失。
    注意：趋势线不含链梯未成熟预扣（cell快照才是考核口径），文案已在页面标注。"""
    today = now_cn.date()
    start = today - timedelta(days=days)
    daily: Dict[Tuple[str, Any], List[float]] = defaultdict(lambda: [0.0, 0.0])  # (scope,date)->[sale,net]
    for o in parsed:
        d = datetime.fromtimestamp(o["order_ts"] / 1000, tz=CN_TZ).date()
        if d < start or d > today:
            continue
        net = o["profit"] if not o["is_return"] else -_order_expected_loss(o, rates)
        for scope in ("公司", o["operator"]):
            agg = daily[(scope, d)]
            agg[0] += o["sale"]
            agg[1] += net

    scopes = sorted(set(s for s, _ in daily.keys()))
    rows = []
    for scope in scopes:
        for i in range(days + 1):
            d = start + timedelta(days=i)
            sale_1d, net_1d = daily.get((scope, d), [0.0, 0.0])
            w_sale = w_net = 0.0
            for j in range(30):
                dj = d - timedelta(days=j)
                if dj < start - timedelta(days=30):
                    break
                s2 = daily.get((scope, dj))
                if s2:
                    w_sale += s2[0]
                    w_net += s2[1]
            margin = (w_net / w_sale) if w_sale > 0 else None
            rows.append((scope, d, round(sale_1d, 2), round(net_1d, 2),
                         round(w_sale, 2), round(w_net, 2),
                         round(margin, 4) if margin is not None else None))
    sql = """
        INSERT INTO order_system.profit_trend_daily
            (scope, stat_date, sale_1d, net_1d, rolling30_sale, rolling30_net, rolling30_margin)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE sale_1d=VALUES(sale_1d), net_1d=VALUES(net_1d),
            rolling30_sale=VALUES(rolling30_sale), rolling30_net=VALUES(rolling30_net),
            rolling30_margin=VALUES(rolling30_margin)
    """
    with conn.cursor() as cur:
        for i in range(0, len(rows), 500):
            cur.executemany(sql, rows[i:i + 500])
    conn.commit()
    return len(rows)


# =====================================================================
# SKU 90天指标表（行动清单/后续控制回路的数据源）
# =====================================================================

def build_sku_metrics(conn, parsed: List[Dict], rates, now_cn: datetime) -> int:
    cut90 = (now_cn - timedelta(days=90)).timestamp() * 1000
    agg: Dict[Tuple[str, str], Dict[str, Any]] = defaultdict(lambda: {
        "operator": "", "supplier": "", "orders": 0, "sale": 0.0,
        "profit_gross": 0.0, "returns": 0, "loss_expected": 0.0})
    for o in parsed:
        if o["order_ts"] <= cut90 or not o["sku"]:
            continue
        a = agg[(o["sku"], o["store"])]
        a["operator"] = o["operator"]
        a["supplier"] = o["supplier"]
        a["orders"] += 1
        a["sale"] += o["sale"]
        if o["is_return"]:
            a["returns"] += 1
            a["loss_expected"] += _order_expected_loss(o, rates)
        else:
            a["profit_gross"] += o["profit"]
    rows = []
    for (sku, store), a in agg.items():
        net = a["profit_gross"] - a["loss_expected"]
        margin = (net / a["sale"]) if a["sale"] > 0 else None
        rows.append((sku, store, a["operator"], a["supplier"], a["orders"],
                     round(a["sale"], 2), round(a["profit_gross"], 2), a["returns"],
                     round(a["loss_expected"], 2), round(net, 2),
                     round(margin, 4) if margin is not None else None))
    with conn.cursor() as cur:
        cur.execute("DELETE FROM order_system.profit_sku_90d")
        sql = """
            INSERT INTO order_system.profit_sku_90d
                (shop_sku, store, operator, supplier, orders, sale, profit_gross,
                 returns_cnt, loss_expected, net, margin)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """
        for i in range(0, len(rows), 500):
            cur.executemany(sql, rows[i:i + 500])
    conn.commit()
    return len(rows)


# =====================================================================
# 月度 cohort（订单月 × 运营：毛利 与 被退货侵蚀后的净利）
# =====================================================================

def _order_actual_loss(o) -> float:
    """到目前为止的实际口径损失：供应商退款只认已回填到账的；没到账的按全损（钱没回来就不算）。"""
    if not o["is_return"]:
        return 0.0
    refund = o["supplier_refund"]
    if refund is not None and refund > 0:
        return max(0.0, o["cost"] - refund) + o["return_fee"]
    return o["cost"] + o["return_fee"]


def build_month_cohort(conn, parsed: List[Dict], rates, now_cn: datetime,
                       months_back_days: int = 400) -> int:
    """粒度：订单月 × 运营 × 店铺（诊断页要下钻到cell）。"""
    cutoff_ts = (now_cn - timedelta(days=months_back_days)).timestamp() * 1000
    agg: Dict[Tuple[str, str, str], Dict[str, float]] = defaultdict(lambda: {
        "orders": 0, "sale": 0.0, "profit_gross": 0.0, "gross_est": 0.0,
        "returns_cnt": 0, "loss_expected": 0.0, "loss_actual": 0.0,
        "neg_profit": 0.0, "neg_n": 0})
    neg_orders = []   # 亏本卖的正常单明细(诊断"查定价"处方展开用)
    for o in parsed:
        if o["order_ts"] <= cutoff_ts:
            continue
        month = datetime.fromtimestamp(o["order_ts"] / 1000, tz=CN_TZ).strftime("%Y-%m")
        a = agg[(month, o["operator"], o["store"])]
        a["orders"] += 1
        a["sale"] += o["sale"]
        if o["is_return"]:
            a["returns_cnt"] += 1
            a["loss_expected"] += _order_expected_loss(o, rates)
            a["loss_actual"] += _order_actual_loss(o)
        else:
            a["profit_gross"] += o["profit"]
            if not o["use_actual"]:
                a["gross_est"] += o["profit"]   # 账单未到/残缺,暂按预估占位的部分
            if o["profit"] < 0:
                a["neg_profit"] += o["profit"]  # 亏本卖的正常单(诊断用)
                a["neg_n"] += 1
                neg_orders.append((
                    month, o["operator"], o["store"], o["order_id"], o["sku"],
                    datetime.fromtimestamp(o["order_ts"] / 1000, tz=CN_TZ).date(),
                    round(o["sale"], 2), round(o["cost"], 2), round(o["profit"], 2),
                    1 if o["use_actual"] else 0))
    rows = []
    for (month, operator, store), a in agg.items():
        net = a["profit_gross"] - a["loss_expected"]
        net_actual = a["profit_gross"] - a["loss_actual"]
        rows.append((month, operator, store, a["orders"], round(a["sale"], 2),
                     round(a["profit_gross"], 2), a["returns_cnt"],
                     round(a["loss_expected"], 2), round(net, 2),
                     round(a["profit_gross"] / a["sale"], 4) if a["sale"] > 0 else None,
                     round(net / a["sale"], 4) if a["sale"] > 0 else None,
                     round(a["loss_actual"], 2), round(net_actual, 2),
                     round(net_actual / a["sale"], 4) if a["sale"] > 0 else None,
                     round(a["gross_est"], 2),
                     round(a["neg_profit"], 2), a["neg_n"]))
    with conn.cursor() as cur:
        cur.execute("DELETE FROM order_system.profit_month_cohort")
        sql = """
            INSERT INTO order_system.profit_month_cohort
                (order_month, operator, store, orders, sale, profit_gross, returns_cnt,
                 loss_expected, net, margin_gross, margin_net,
                 loss_actual, net_actual, margin_net_actual, gross_est,
                 neg_profit, neg_n)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """
        for i in range(0, len(rows), 500):
            cur.executemany(sql, rows[i:i + 500])
        cur.execute("DELETE FROM order_system.profit_neg_orders")
        neg_sql = """
            INSERT INTO order_system.profit_neg_orders
                (order_month, operator, store, order_id, shop_sku, order_date,
                 sale, cost, profit, is_actual)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """
        for i in range(0, len(neg_orders), 500):
            cur.executemany(neg_sql, neg_orders[i:i + 500])
    conn.commit()
    return len(rows)


# =====================================================================
# 问题规则引擎
# =====================================================================

def run_issue_rules(conn, snapshots: List[Dict], parsed: List[Dict],
                    rates, now_cn: datetime) -> List[Dict[str, Any]]:
    today = now_cn.date()
    issues: List[Dict[str, Any]] = []

    def add(issue_type, entity, severity, impact, evidence, suggestion):
        issues.append({
            "detected_date": today, "issue_type": issue_type, "entity": entity,
            "severity": severity, "impact_usd": round(impact, 2),
            "evidence": evidence, "suggestion": suggestion,
        })

    # R1 cell 破线
    for s in snapshots:
        if s["sale_90d"] < CELL_MIN_SALE_FOR_ISSUE or s["margin_90d_adj"] is None:
            continue
        if s["margin_90d_adj"] < BASELINE_MARGIN:
            sev = "high" if s["margin_90d_adj"] < BASELINE_MARGIN - 0.03 else "mid"
            add("cell_below_baseline", f"{s['operator']}-{s['store']}", sev, s["gap_usd"],
                (f"90天修正利润率 {s['margin_90d_adj']*100:.2f}%（毛利率 "
                 f"{(s['margin_90d_raw'] or 0)*100:.2f}%，确认退货损失 ${s['confirmed_return_loss_90d']:,.0f}，"
                 f"待追敞口 ${s['pending_exposure_90d']:,.0f}，未成熟预扣 ${s['expected_future_loss_90d']:,.0f}）"),
                f"缺口 ${s['gap_usd']:,.0f}：优先处理该 cell 的负期望SKU与追款清单")

    # R2 负期望 SKU（90天净贡献为负）
    cut90 = (now_cn - timedelta(days=90)).timestamp() * 1000
    sku_agg: Dict[Tuple[str, str], Dict[str, float]] = defaultdict(
        lambda: {"profit": 0.0, "loss": 0.0, "returns": 0, "orders": 0})
    for o in parsed:
        if o["order_ts"] <= cut90 or not o["sku"]:
            continue
        a = sku_agg[(o["sku"], o["store"])]
        a["orders"] += 1
        if o["is_return"]:
            a["returns"] += 1
            rr = _recovery_rate(rates, o["store"], o["supplier"])
            refund = o["supplier_refund"]
            if refund is not None and refund > 0:
                a["loss"] += max(0.0, o["cost"] - refund) + o["return_fee"]
            else:
                a["loss"] += o["cost"] * (1.0 - rr) + o["return_fee"]
        else:
            a["profit"] += o["profit"]
    for (sku, store), a in sku_agg.items():
        net = a["profit"] - a["loss"]
        if a["returns"] >= 2 and net < NEG_EV_SKU_THRESHOLD:
            add("negative_ev_sku", f"{sku}@{store}",
                "high" if net < -200 else "mid", -net,
                f"90天 {a['orders']:.0f}单/{a['returns']:.0f}退，非退货毛利 ${a['profit']:,.0f}，"
                f"期望退货损失 ${a['loss']:,.0f}，净贡献 ${net:,.0f}",
                "建议下架或大幅提价（并核对listing是否引导了错误预期）")

    # R3 追款超期（按店铺聚合）
    with conn.cursor() as cur:
        cur.execute("""
            SELECT store, COUNT(*) AS n, SUM(exposure) AS expo
            FROM order_system.return_case
            WHERE state='pending' AND age_days > %s AND cost >= 50
            GROUP BY store
        """, (RECOVERY_OVERDUE_DAYS,))
        for row in cur.fetchall():
            add("recovery_overdue", row["store"], "mid", float(row["expo"] or 0),
                f"{row['n']}笔退货超{RECOVERY_OVERDUE_DAYS}天无供应商退款回填，货值敞口 ${float(row['expo'] or 0):,.0f}",
                "导出追款清单给财务与供应商对账（回填「供应商退款」后自动核销）")

    # R4 数据新鲜度：实际成本回填滞后
    cut_lo = (now_cn - timedelta(days=90)).timestamp() * 1000
    cut_hi = (now_cn - timedelta(days=BACKFILL_STALE_DAYS)).timestamp() * 1000
    n_all, n_filled = 0, 0
    for o in parsed:
        if cut_lo < o["order_ts"] <= cut_hi:
            n_all += 1
            if o["actual_cost_filled"]:
                n_filled += 1
    if n_all >= 200:
        fill_rate = n_filled / n_all
        if fill_rate < 0.5:
            add("data_stale", "订单表-实际成本回填", "low", 0,
                f"{BACKFILL_STALE_DAYS}-90天前订单的实际成本回填率仅 {fill_rate*100:.0f}%（{n_filled}/{n_all}）",
                "提醒财务导入最新月度账单；期间相关指标以预估口径为主")

    # R5 退货异动（近7天 vs 前4周周均）
    ret_by_store_week: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
    cut7 = (now_cn - timedelta(days=7)).timestamp() * 1000
    cut35 = (now_cn - timedelta(days=35)).timestamp() * 1000
    for o in parsed:
        if not o["is_return"]:
            continue
        if o["order_ts"] > cut7:
            ret_by_store_week[o["store"]][0] += 1
        elif o["order_ts"] > cut35:
            ret_by_store_week[o["store"]][1] += 1
    for store, (w1, w4) in ret_by_store_week.items():
        weekly_avg = w4 / 4.0
        if w1 >= 5 and weekly_avg > 0 and w1 > 2 * weekly_avg:
            add("return_spike", store, "mid", 0,
                f"近7天退货 {w1} 笔，前4周周均 {weekly_avg:.1f} 笔（按下单日口径，实际还会滞后放大）",
                "检查该店近期上架/改价/物流是否有异常")

    # R6 已点"已下架"但之后仍有销量（店铺可能没真正下架）
    with conn.cursor() as cur:
        cur.execute("""SELECT id, target, store, created_at FROM order_system.action_log
                       WHERE action_type='delist' AND status='executed'""")
        delist_rows = cur.fetchall()
    for row in delist_rows:
        target = row["target"] if isinstance(row, dict) else row[1]
        store_d = row["store"] if isinstance(row, dict) else row[2]
        created = row["created_at"] if isinstance(row, dict) else row[3]
        sku_d = target.split("@")[0]
        mark_ts = created.timestamp() * 1000 if created else 0
        sold = [o for o in parsed
                if o["sku"] == sku_d and o["store"] == store_d and o["order_ts"] > mark_ts]
        if sold:
            add("delisted_but_selling", target, "high", sum(o["sale"] for o in sold),
                f"已于 {created:%m-%d %H:%M} 标记下架，此后仍有 {len(sold)} 单成交、"
                f"销售额 ${sum(o['sale'] for o in sold):,.0f}",
                "店铺可能没有真正下架——去平台后台核实 offer 状态并下架")

    # 写库：当天同 (type, entity) 幂等；昨天仍 open 但今天未再检出的自动关闭
    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO order_system.issue_log
                (detected_date, issue_type, entity, severity, impact_usd, evidence, suggestion)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE severity=VALUES(severity),
                impact_usd=VALUES(impact_usd), evidence=VALUES(evidence),
                suggestion=VALUES(suggestion), status=IF(status='resolved','resolved',status)
        """, [(i["detected_date"], i["issue_type"], i["entity"], i["severity"],
               i["impact_usd"], i["evidence"], i["suggestion"]) for i in issues])
        cur.execute("""
            UPDATE order_system.issue_log SET status='stale'
            WHERE status='open' AND detected_date < %s
              AND NOT EXISTS (
                SELECT 1 FROM (SELECT issue_type t, entity e FROM order_system.issue_log
                               WHERE detected_date=%s) today
                WHERE today.t=issue_log.issue_type AND today.e=issue_log.entity)
        """, (today, today))
    conn.commit()
    return issues


# =====================================================================
# 编排入口
# =====================================================================

def run_daily_aggregation() -> Dict[str, Any]:
    started = time.time()
    now_cn = datetime.now(CN_TZ)
    raw = fetch_feishu_orders()
    parsed = parse_orders(raw)

    conn = DBManager.get_connection()
    try:
        return_dates = load_return_dates(conn)
        case_stats = rebuild_return_cases(conn, parsed, return_dates, now_cn)
        case_stats["claim_filed"] = sync_claim_filed(conn)
        rates = compute_recovery_rates(conn)
        maturity = build_maturity_model(parsed, return_dates, now_cn)
        snapshots = build_cell_snapshots(conn, parsed, rates, maturity, now_cn)
        issues = run_issue_rules(conn, snapshots, parsed, rates, now_cn)
        trend_rows = build_trend_daily(conn, parsed, rates, now_cn)
        sku_rows = build_sku_metrics(conn, parsed, rates, now_cn)
        cohort_rows = build_month_cohort(conn, parsed, rates, now_cn)
    finally:
        conn.close()

    return {
        "snapshot_date": str(now_cn.date()),
        "orders_parsed": len(parsed),
        "return_cases": case_stats,
        "cells": len(snapshots),
        "issues_detected": len(issues),
        "trend_rows": trend_rows,
        "sku_rows": sku_rows,
        "cohort_rows": cohort_rows,
        "elapsed_sec": round(time.time() - started, 1),
    }
