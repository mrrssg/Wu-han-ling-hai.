# ============================================================
# ✅ app/services/tracking_service.py  （完整替换版）
# ✅ 只新增必要的 *_file 函数，其它逻辑保持原样
# ============================================================

import os
import pandas as pd
from datetime import datetime
from app.models.tracking_db_manager import Tracking_DBManager
from config import Config

BASE_DIR = Config.BASE_DIR
UPLOAD_DIR = os.path.join(BASE_DIR, "instance", "uploads", "tracking")
EXPORT_DIR = os.path.join(BASE_DIR, "exports", "tracking")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(EXPORT_DIR, exist_ok=True)


def upload_tracking_file(file):
    file.stream.seek(0)  # ✅ 关键：指针回到开头
    df = pd.read_excel(file.stream)

    # ✅ 去掉完全空行（可选）
    df = df.dropna(how="all")

    # ✅ 返回条数
    return len(df)


def read_costway_tracking_excel(file_path):
    df = pd.read_excel(file_path)

    # 你可以根据你的表头改
    # 订单号列名可能是：OrderID / order_id / 订单号
    # 跟踪号列名可能是：TrackingNumber / tracking / 跟踪号
    order_col = None
    order_sku = None
    tracking_col = None

    for col in df.columns:
        if str(col).lower() in ["订单编号", "", "订单号"]:
            order_col = col
        if str(col).lower() in ["trackingnumber", "tracking", "交易时间"]:
            tracking_col = col
        if str(col).lower() in ["SKU", "", ""]:
            order_sku = col

    if not order_col or not tracking_col or not order_sku:
        raise Exception("Excel 缺少必要字段：订单号 / TrackingNumber")

    return df[[order_col, tracking_col, order_sku]].rename(
        columns={order_col: "costwayorder", tracking_col: "tracking", order_sku: "sku"}
    )


# =====================
# 3个平台回传逻辑（你可以把接口写进来）
# =====================
def push_tracking_costway(file_path):
    df = read_costway_tracking_excel(file_path)

    df["sku"] = df["sku"].astype(str).str.strip()
    df["costwayorder"] = df["costwayorder"].astype(str).str.strip()

    macy_data = Tracking_DBManager.research_macyorder()
    walmart_data = Tracking_DBManager.research_walmartorder()

    macy_success, macy_fail = _match_and_update(df, macy_data, "macy")
    wm_success, wm_fail = _match_and_update(df, walmart_data, "walmart")

    return {
        "costway": True,
        "macy_success": macy_success,
        "macy_fail": macy_fail,
        "walmart_success": wm_success,
        "walmart_fail": wm_fail,
    }


def _match_and_update(df, data, platform):
    success = 0
    fail = 0
    updated_orders = []
    matched_rows = []
    for row in data:
        sku = str(row["Costway_SKU"]).strip()
        costwayorder = str(row["CostwayOrder"]).strip()
        order_number = str(row.get("Order number") or row.get("PO_Number") or "").strip()

        match = df[(df["sku"] == sku) & (df["costwayorder"] == costwayorder)]
        if match.empty:
            fail += 1
            continue

        match = match.copy()
        match["order_number"] = order_number  # ✅ 必须带回真实订单号
        matched_rows.append(match)
        if platform == "macy":
            Tracking_DBManager.update_macyorder(match)
            updated_orders.append(order_number)
        elif platform == "walmart":
            Tracking_DBManager.update_walmartorder(match)
            updated_orders.append(order_number)
        elif platform == "bestbuy":
            Tracking_DBManager.update_bestbuyorder(match)
            updated_orders.append(order_number)
        else:
            fail += 1
            continue

        success += 1

    # ✅ 批量利润更新（一次SQL）
    if matched_rows:
        all_match_df = pd.concat(matched_rows, ignore_index=True)

        if platform == "macy":
            Tracking_DBManager.estimated_profit(all_match_df)
        elif platform == "walmart":
            Tracking_DBManager.walmart_estimated_profit(all_match_df)

    return success, fail


def push_tracking_vevor(file_path):
    df = read_tracking_excel(file_path)
    return _push_to_db(df, "vevor")


def push_tracking_dajian(file_path):
    df = read_tracking_excel(file_path)
    return _push_to_db(df, "dajian")


def _push_to_db(df, platform):
    """
    这里先做最简单版本：写入数据库 tracking 表
    你后续如果需要对接 API / Selenium 回传，可在这里扩展
    """
    conn = get_conn()
    cursor = conn.cursor()

    success = 0
    fail = 0

    for _, row in df.iterrows():
        order_id = str(row["order_id"]).strip()
        tracking = str(row["tracking"]).strip()

        try:
            cursor.execute("""
                UPDATE orders
                SET tracking_number=%s, tracking_platform=%s, tracking_updated_at=NOW()
                WHERE order_id=%s
            """, (tracking, platform, order_id))
            success += 1
        except Exception as e:
            fail += 1

    conn.commit()
    cursor.close()
    conn.close()

    return {"platform": platform, "success": success, "fail": fail, "total": len(df)}


# =====================
# 导出不同渠道 tracking
# =====================
def export_tracking_file(channel):
    conn = get_conn()
    df = None

    if channel == "macy_kuyotq":
        sql = "SELECT order_id, sku, tracking_number, carrier, ship_date FROM orders WHERE platform='macy' AND store='kuyotq' AND tracking_number IS NOT NULL"
    elif channel == "macy_wopet":
        sql = "SELECT order_id, sku, tracking_number, carrier, ship_date FROM orders WHERE platform='macy' AND store='wopet' AND tracking_number IS NOT NULL"
    elif channel == "bestbuy_top":
        sql = "SELECT order_id, sku, tracking_number, carrier, ship_date FROM orders WHERE platform='bestbuy' AND store='top' AND tracking_number IS NOT NULL"
    elif channel == "bestbuy_del":
        sql = "SELECT order_id, sku, tracking_number, carrier, ship_date FROM orders WHERE platform='bestbuy' AND store='del' AND tracking_number IS NOT NULL"
    else:
        raise Exception("未知导出渠道")

    df = pd.read_sql(sql, conn)
    conn.close()

    export_path = os.path.join(EXPORT_DIR, f"{channel}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx")
    df.to_excel(export_path, index=False)
    return export_path


# ============================================================
# ✅ ✅ ✅ 下面是“必须新增”的部分（只新增，不动你原逻辑）
# ============================================================

def _read_excel_from_upload(file):
    file.stream.seek(0)
    df = pd.read_excel(file.stream)
    df = df.dropna(how="all")
    return df


def _read_costway_style_excel_from_upload(file):
    df = _read_excel_from_upload(file)

    order_col = None
    order_sku = None
    tracking_col = None

    for col in df.columns:
        col_name = str(col).strip().lower()

        if col_name in [
            "订单编号",
            "order_id",
            "orderid",
            "order number",
            "order_number",
            "costwayorder",
            "costway_order",
            "costway order",
        ]:
            order_col = col

        if col_name in [
            "trackingnumber",
            "tracking_number",
            "tracking number",
            "tracking",
            "追踪号",
            "跟踪号",
            "交易时间",
        ]:
            tracking_col = col

        if col_name in ["sku", "SKU", "offer sku", "offer_sku"]:
            order_sku = col

    if not order_col or not tracking_col or not order_sku:
        raise Exception(
            f"Excel 缺少必要字段：订单号 / SKU / TrackingNumber，当前列：{df.columns.tolist()}"
        )

    df = df[[order_col, tracking_col, order_sku]].rename(
        columns={order_col: "costwayorder", tracking_col: "tracking", order_sku: "sku"}
    )
    return df


def _read_orderid_tracking_excel(file):
    df = _read_excel_from_upload(file)
    col_map = {str(c).strip().lower(): c for c in df.columns}

    order_col = None
    for key in [
        "order_id",
        "orderid",
        "order number",
        "order_number",
        "po_number",
        "po number",
        "\u8ba2\u5355\u53f7",
        "\u8ba2\u5355\u7f16\u53f7",
    ]:
        if key in col_map:
            order_col = col_map[key]
            break

    tracking_col = None
    for key in [
        "trackingnumber",
        "tracking_number",
        "tracking number",
        "tracking",
        "\u8ffd\u8e2a\u53f7",
    ]:
        if key in col_map:
            tracking_col = col_map[key]
            break

    if not order_col or not tracking_col:
        raise Exception("Excel must contain order_id and TrackingNumber columns")

    df = df[[order_col, tracking_col]].rename(
        columns={order_col: "order_id", tracking_col: "tracking"}
    )
    df["order_id"] = df["order_id"].astype(str).str.strip()
    df["tracking"] = df["tracking"].astype(str).str.strip()
    df = df[(df["order_id"] != "") & (df["order_id"].str.lower() != "nan")]
    return df


def _read_orderid_tracking_text(text: str):
    rows = []
    if not text:
        return pd.DataFrame(columns=["order_id", "tracking"])

    def looks_like_order_id(val: str) -> bool:
        v = val.strip()
        if "-" in v:
            return True
        if any(ch.isalpha() for ch in v):
            return True
        return False

    def looks_like_tracking(val: str) -> bool:
        v = val.strip().upper()
        if v.startswith("1Z") or v.startswith("D1"):
            return True
        return v.isdigit()

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("\t") if p.strip()]
        if len(parts) < 2:
            continue
        a, b = parts[0], parts[1]
        if looks_like_order_id(a) and looks_like_tracking(b):
            order_id, tracking = a, b
        elif looks_like_order_id(b) and looks_like_tracking(a):
            order_id, tracking = b, a
        else:
            # fallback: assume first is order_id, second is tracking
            order_id, tracking = a, b
        rows.append({"order_id": order_id, "tracking": tracking})

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["order_id"] = df["order_id"].astype(str).str.strip()
    df["tracking"] = df["tracking"].astype(str).str.strip()
    df = df[(df["order_id"] != "") & (df["order_id"].str.lower() != "nan")]
    return df


def _fetch_macy_sku_map(order_ids):
    if not order_ids:
        return {}
    conn = Tracking_DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            placeholders = ",".join(["%s"] * len(order_ids))
            sql = (
                "SELECT `Order number` AS order_id, Costway_SKU AS sku "
                f"FROM macyorder WHERE `Order number` IN ({placeholders})"
            )
            cursor.execute(sql, order_ids)
            rows = cursor.fetchall()
            return {str(r["order_id"]).strip(): (r.get("sku") or "") for r in rows}
    finally:
        conn.close()


def _fetch_walmart_sku_map(order_ids):
    if not order_ids:
        return {}
    conn = Tracking_DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            placeholders = ",".join(["%s"] * len(order_ids))
            sql = (
                "SELECT PO_Number AS order_id, Costway_SKU AS sku "
                f"FROM walmartorder WHERE PO_Number IN ({placeholders})"
            )
            cursor.execute(sql, order_ids)
            rows = cursor.fetchall()
            return {str(r["order_id"]).strip(): (r.get("sku") or "") for r in rows}
    finally:
        conn.close()


def _fetch_bestbuy_sku_map(order_ids):
    if not order_ids:
        return {}
    conn = Tracking_DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            placeholders = ",".join(["%s"] * len(order_ids))
            sql = (
                "SELECT `Order number` AS order_id, Costway_SKU AS sku "
                f"FROM bestbuyorder WHERE `Order number` IN ({placeholders})"
            )
            cursor.execute(sql, order_ids)
            rows = cursor.fetchall()
            return {str(r["order_id"]).strip(): (r.get("sku") or "") for r in rows}
    finally:
        conn.close()


def _push_tracking_all_platforms_by_order_id_df(df):
    order_ids = df["order_id"].dropna().astype(str).str.strip().unique().tolist()

    macy_sku_map = _fetch_macy_sku_map(order_ids)
    walmart_sku_map = _fetch_walmart_sku_map(order_ids)
    bestbuy_sku_map = _fetch_bestbuy_sku_map(order_ids)

    macy_rows = []
    walmart_rows = []
    bestbuy_rows = []

    for _, row in df.iterrows():
        oid = str(row["order_id"]).strip()
        tracking = str(row["tracking"]).strip()

        if oid in macy_sku_map:
            macy_rows.append(
                {"order_number": oid, "tracking": tracking, "sku": macy_sku_map.get(oid, "")}
            )
        if oid in walmart_sku_map:
            walmart_rows.append(
                {"order_number": oid, "unnamed:_2": tracking, "sku": walmart_sku_map.get(oid, "")}
            )
        if oid in bestbuy_sku_map:
            bestbuy_rows.append(
                {"order_number": oid, "tracking": tracking, "sku": bestbuy_sku_map.get(oid, "")}
            )

    macy_success = 0
    walmart_success = 0
    bestbuy_success = 0
    if macy_rows:
        macy_df = pd.DataFrame(macy_rows)
        Tracking_DBManager.update_macyorder(macy_df)
        Tracking_DBManager.estimated_profit(macy_df)
        macy_success = len(macy_rows)

    if walmart_rows:
        walmart_df = pd.DataFrame(walmart_rows)
        Tracking_DBManager.update_walmartorder(walmart_df)
        Tracking_DBManager.walmart_estimated_profit(walmart_df)
        walmart_success = len(walmart_rows)

    if bestbuy_rows:
        bestbuy_df = pd.DataFrame(bestbuy_rows)
        Tracking_DBManager.update_bestbuyorder(bestbuy_df)
        bestbuy_success = len(bestbuy_rows)

    return {
        "macy_success": macy_success,
        "macy_fail": max(len(df) - macy_success, 0),
        "walmart_success": walmart_success,
        "walmart_fail": max(len(df) - walmart_success, 0),
        "bestbuy_success": bestbuy_success,
        "bestbuy_fail": max(len(df) - bestbuy_success, 0),
    }


def push_tracking_all_platforms_by_order_id(file):
    df = _read_orderid_tracking_excel(file)
    return _push_tracking_all_platforms_by_order_id_df(df)


def push_tracking_all_platforms_by_text(text: str):
    df = _read_orderid_tracking_text(text)
    if df.empty:
        return {"error": "no_rows"}
    return _push_tracking_all_platforms_by_order_id_df(df)


def push_tracking_costway_file(file):
    """
    ✅ 不保存，直接读取上传文件，然后复用 costway 回传逻辑
    """
    df = _read_costway_style_excel_from_upload(file)

    df["sku"] = df["sku"].astype(str).str.strip()
    df["costwayorder"] = df["costwayorder"].astype(str).str.strip()

    macy_data = Tracking_DBManager.research_macyorder()
    walmart_data = Tracking_DBManager.research_walmartorder()
    bestbuy_data = Tracking_DBManager.research_bestbuyorder()

    macy_success, macy_fail = _match_and_update(df, macy_data, "macy")
    wm_success, wm_fail = _match_and_update(df, walmart_data, "walmart")
    bb_success, bb_fail = _match_and_update(df, bestbuy_data, "bestbuy")

    return {
        "costway": True,
        "macy_success": macy_success,
        "macy_fail": macy_fail,
        "walmart_success": wm_success,
        "walmart_fail": wm_fail,
        "bestbuy_success": bb_success,
        "bestbuy_fail": bb_fail,
    }


def push_tracking_vevor_file(file):
    return push_tracking_all_platforms_by_order_id(file)

def push_tracking_dajian_file(file):
    return push_tracking_all_platforms_by_order_id(file)

