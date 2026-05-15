"""
Business orchestration for "HD Vevor 发货" page.

Pipeline (per order):
  Teapplix OrderNotification → take OrderItems[0].Name (shop SKU)
  → autooperate.mapping_table → warehouse_SKU
  → newestdropship_vevor → confirm Vevor + read Stock_W10 / Stock_W432
  → choose warehouse (W10>=W432 → NJ3, else CA6) → resolve ProfileId
  → Feishu HD-TOP-Mirkal → length/width/height/weight (ceil)
  → POST Teapplix PurchaseLabelForOrder (UPS_GROUND)
  → persist hd_label_records + write back order tracking
  → produce the 35-column Excel TSV row for one-click copy
"""
import json
import logging
import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.models.db_manager import DBManager
from app.services import teapplix_label_service as tp
from app.services import feishu_dims_service as fs


log = logging.getLogger(__name__)


# -- Constants ----------------------------------------------------------------

DEFAULT_METHOD = "UPS_GROUND"

# StoreKey + warehouse-column → Teapplix ProfileId
PROFILE_MAP: Dict[Tuple[str, str], int] = {
    ("TOP", "W10"): 11,    # HD-Topia-Vevor-T3342-NJ3
    ("TOP", "W432"): 12,   # HD-Topia-Vevor-T3342-CA6
    ("DEL", "W10"): 13,    # HD-Delphi-Vevor-T3342-NJ3
    ("DEL", "W432"): 14,   # HD-Delphi-Vevor-T3342-CA6
}

# Vevor warehouse-id (Excel column R) + shipping-channel-id (column S)
WAREHOUSE_ID_MAP = {"W10": 10, "W432": 432}
CHANNEL_ID_MAP = {"W10": 3808, "W432": 3801}

# Fixed Excel field values (column letter → value)
FIXED_FIELDS = {
    "D": "US",              # 站点
    "E": "USD",             # 币种
    "F": "DSGCT",           # 促销码
    "G": "Bank Transfer",   # 支付方式
    "L": "0",               # 商品价格税
    "M": "消费税",          # 税种
    "N": "0",               # 税率%
    "O": "0",               # 折扣税金额
    "P": "0",               # 折扣金额（含税）
    "Q": "0",               # 运费
    "W": "VincentHelinas@outlook.com",  # 收货邮箱
    "X": "United States",   # 国家
    "Y": "US",              # 国家简码
    "AG": "wuhanlinghai@163.com",  # 用户邮箱
}

BATCH_LIMIT = 50  # frontend hard limit; Teapplix has no documented bulk endpoint


# -- DB helpers ---------------------------------------------------------------

def _fetchall(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            if params:
                cur.execute(sql, params)
            else:
                cur.execute(sql)
            return cur.fetchall() or []
    finally:
        conn.close()


def _fetchone(sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
    rows = _fetchall(sql, params)
    return rows[0] if rows else None


def _exec(sql: str, params: tuple = ()) -> None:
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_existing_record(txn_id: str) -> Optional[Dict[str, Any]]:
    return _fetchone("SELECT * FROM hd_label_records WHERE txn_id=%s", (txn_id,))


def get_existing_records(txn_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    if not txn_ids:
        return {}
    placeholders = ",".join(["%s"] * len(txn_ids))
    rows = _fetchall(
        f"SELECT * FROM hd_label_records WHERE txn_id IN ({placeholders})",
        tuple(txn_ids),
    )
    return {r["txn_id"]: r for r in rows}


def lookup_warehouse_sku(shop_sku: str) -> Optional[Dict[str, Any]]:
    """shop_sku → mapping_table → {warehouse_SKU, owner}"""
    return _fetchone(
        "SELECT SKU AS shop_sku, warehouse_SKU AS warehouse_sku, owner "
        "FROM mapping_table WHERE SKU=%s",
        (shop_sku,),
    )


def lookup_vevor_stock(warehouse_sku: str) -> Optional[Dict[str, Any]]:
    """warehouse_SKU → newestdropship_vevor row, or None if not Vevor."""
    return _fetchone(
        "SELECT SKU AS warehouse_sku, Price, Stock, Stock_W10, Stock_W432, Updated_At "
        "FROM newestdropship_vevor WHERE SKU=%s",
        (warehouse_sku,),
    )


def lookup_warehouse_skus(shop_skus: List[str]) -> Dict[str, Dict[str, Any]]:
    if not shop_skus:
        return {}
    placeholders = ",".join(["%s"] * len(shop_skus))
    rows = _fetchall(
        f"SELECT SKU AS shop_sku, warehouse_SKU AS warehouse_sku, owner "
        f"FROM mapping_table WHERE SKU IN ({placeholders})",
        tuple(shop_skus),
    )
    return {r["shop_sku"]: r for r in rows}


def lookup_vevor_stocks(warehouse_skus: List[str]) -> Dict[str, Dict[str, Any]]:
    if not warehouse_skus:
        return {}
    placeholders = ",".join(["%s"] * len(warehouse_skus))
    rows = _fetchall(
        f"SELECT SKU AS warehouse_sku, Price, Stock, Stock_W10, Stock_W432, Updated_At "
        f"FROM newestdropship_vevor WHERE SKU IN ({placeholders})",
        tuple(warehouse_skus),
    )
    return {r["warehouse_sku"]: r for r in rows}


# -- Order enrichment ---------------------------------------------------------

def _normalize_store_key(raw: Any) -> str:
    s = (raw or "").strip().upper()
    if s in {"TOP", "DEL"}:
        return s
    # Legacy "hd" / "" → treat as TOP by default; UI shows the raw value
    return s or "TOP"


def _pick_warehouse(stock_w10: int, stock_w432: int) -> str:
    """W10 >= W432 → 'W10' (NJ3), otherwise 'W432' (CA6)."""
    if (stock_w10 or 0) >= (stock_w432 or 0):
        return "W10"
    return "W432"


def _resolve_profile_id(store_key: str, warehouse_col: str) -> Optional[int]:
    return PROFILE_MAP.get((store_key, warehouse_col))


def enrich_orders(orders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """For each Teapplix order, attach Vevor / warehouse / dims / status info.

    Output rows include:
      txn_id, store_key, raw_store_key, shop_sku, warehouse_sku, supplier,
      qty, total, recipient_*, profile_id, warehouse_id, channel_id, dims_*,
      stock_w10, stock_w432, is_vevor, has_dims, already_shipped,
      existing_status (when applicable), warnings: [..]
    """
    # 1) collect SKUs we need to look up
    shop_skus: List[str] = []
    for o in orders:
        items = o.get("OrderItems") or []
        if items:
            sku = items[0].get("Name")
            if sku:
                shop_skus.append(sku)

    mapping = lookup_warehouse_skus(shop_skus)
    warehouse_skus = [m["warehouse_sku"] for m in mapping.values() if m.get("warehouse_sku")]
    vevor_rows = lookup_vevor_stocks(warehouse_skus)
    dims_map = {}
    try:
        dims_map = fs.fetch_dims_for_shop_skus(shop_skus)
    except Exception as e:
        log.warning("Feishu dims fetch failed (will show as missing in UI): %s", e)

    # already-purchased labels from our DB
    existing_map = get_existing_records([o.get("TxnId") for o in orders if o.get("TxnId")])

    # Self-healing sync: if a txn appears in TP's unshipped list while we have
    # status='success' locally, the label was cancelled in the TP UI (or the
    # purchase was rolled back). Reconcile by marking those rows cancelled so
    # the index page no longer claims they're shipped.
    stale_success = [
        txn for txn, rec in existing_map.items()
        if rec and rec.get("status") == "success"
    ]
    if stale_success:
        log.info("Auto-sync: %d local-success txn(s) reappeared in TP unshipped list, "
                 "marking cancelled: %s", len(stale_success), stale_success)
        placeholders = ",".join(["%s"] * len(stale_success))
        try:
            conn = DBManager.get_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        f"UPDATE hd_label_records "
                        f"   SET status='cancelled', cancelled_at=NOW(), "
                        f"       cancel_reason=CONCAT(COALESCE(cancel_reason,''), "
                        f"           ' [auto-synced: reappeared in TP unshipped list]') "
                        f" WHERE status='success' AND txn_id IN ({placeholders})",
                        tuple(stale_success),
                    )
                conn.commit()
            finally:
                conn.close()
            # Reflect the change in the in-memory map so the UI doesn't show "已发"
            for txn in stale_success:
                if existing_map.get(txn):
                    existing_map[txn]["status"] = "cancelled"
        except Exception as e:
            log.warning("Auto-sync UPDATE failed: %s", e)

    rows: List[Dict[str, Any]] = []
    for o in orders:
        txn = o.get("TxnId") or ""
        items = o.get("OrderItems") or []
        first = (items[0] or {}) if items else {}
        shop_sku = first.get("Name") or ""
        qty = int(first.get("Quantity") or 1)
        totals = o.get("OrderTotals") or {}
        to = o.get("To") or {}
        warnings: List[str] = []

        store_raw = o.get("StoreKey") or ""
        store = _normalize_store_key(store_raw)

        # mapping_table → warehouse_sku
        mp = mapping.get(shop_sku) or {}
        warehouse_sku = (mp.get("warehouse_sku") or "").strip()
        if not warehouse_sku:
            warnings.append("mapping_table 没找到 SKU")

        # supplier confirmation
        vevor = vevor_rows.get(warehouse_sku) if warehouse_sku else None
        is_vevor = vevor is not None
        if not is_vevor and warehouse_sku:
            warnings.append("非 Vevor 货（newestdropship_vevor 不命中）")

        stock_w10 = int((vevor or {}).get("Stock_W10") or 0)
        stock_w432 = int((vevor or {}).get("Stock_W432") or 0)
        if is_vevor and stock_w10 == 0 and stock_w432 == 0:
            warnings.append("Vevor 两仓库存皆为 0")

        warehouse_col = _pick_warehouse(stock_w10, stock_w432)
        profile_id = _resolve_profile_id(store, warehouse_col)
        if not profile_id:
            warnings.append(f"未知店铺 '{store_raw}'，无法选 Profile")

        # dims
        dims = dims_map.get(shop_sku) or {}
        has_dims = bool(dims.get("weight_lb_ceil") and dims.get("length_in_ceil"))
        if not has_dims:
            warnings.append("飞书未查到包装重量/尺寸")

        existing = existing_map.get(txn)
        existing_status = (existing or {}).get("status")

        rows.append({
            "txn_id": txn,
            "raw_store_key": store_raw,
            "store_key": store,
            "shop_sku": shop_sku,
            "warehouse_sku": warehouse_sku,
            "supplier": "Vevor" if is_vevor else None,
            "owner": mp.get("owner"),
            "quantity": qty,
            "order_total": float(totals.get("Total") or 0),
            "recipient_name": to.get("Name") or "",
            "recipient_street": to.get("Street") or "",
            "recipient_street2": to.get("Street2") or "",
            "recipient_city": to.get("City") or "",
            "recipient_state": to.get("State") or "",
            "recipient_zip": to.get("ZipCode") or "",
            "recipient_phone": to.get("PhoneNumber") or "",
            "warehouse_col": warehouse_col,
            "warehouse_id": WAREHOUSE_ID_MAP.get(warehouse_col),
            "shipping_channel_id": CHANNEL_ID_MAP.get(warehouse_col),
            "profile_id": profile_id,
            "stock_w10": stock_w10,
            "stock_w432": stock_w432,
            "length_in": dims.get("length_in"),
            "width_in": dims.get("width_in"),
            "depth_in": dims.get("depth_in"),
            "weight_lb": dims.get("weight_lb"),
            "length_in_ceil": dims.get("length_in_ceil"),
            "width_in_ceil": dims.get("width_in_ceil"),
            "depth_in_ceil": dims.get("depth_in_ceil"),
            "weight_lb_ceil": dims.get("weight_lb_ceil"),
            "is_vevor": is_vevor,
            "has_dims": has_dims,
            "is_eligible": bool(is_vevor and has_dims and profile_id),
            "existing_status": existing_status,
            "warnings": warnings,
            "payment_date": (o.get("OrderDetails") or {}).get("PaymentDate"),
        })
    return rows


# -- Excel TSV row builder ----------------------------------------------------

def _fmt_num(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        if v.is_integer():
            return str(int(v))
        return f"{v:.2f}"
    return str(v)


def build_excel_tsv(enriched: Dict[str, Any],
                    tracking_number: Optional[str] = None,
                    label_url: Optional[str] = None) -> str:
    """Produce the 35-column tab-separated row, ready to paste into Vevor信息.xlsx.

    Column B (订单号), AH (折扣率), AI (商品折扣含税单价), V (收货人姓), AB (详细地址2)
    are intentionally left empty (user fills B; the rest are blank in source).
    """
    total = enriched.get("order_total") or 0
    qty = enriched.get("quantity") or 1
    unit_price = (total / qty) if qty else total
    # Use the actual payment date if Teapplix has one, else now() with fixed 09:00:00
    pay_raw = enriched.get("payment_date") or ""
    try:
        # Teapplix returns "YYYY-MM-DD HH:MM:SS"; we just keep the date part + 09:00:00
        ship_dt = pay_raw.split(" ")[0] + " 09:00:00" if pay_raw else \
                  datetime.now().strftime("%Y-%m-%d 09:00:00")
    except Exception:
        ship_dt = datetime.now().strftime("%Y-%m-%d 09:00:00")

    cols = [
        enriched.get("txn_id") or "",                # A 第三方订单号
        "",                                          # B 订单号 (user fills)
        _fmt_num(total),                             # C 订单金额
        FIXED_FIELDS["D"],                           # D 站点
        FIXED_FIELDS["E"],                           # E 币种
        FIXED_FIELDS["F"],                           # F 促销码
        FIXED_FIELDS["G"],                           # G 支付方式
        ship_dt,                                     # H 生单时间
        str(qty),                                    # I 商品数量
        _fmt_num(unit_price),                        # J 商品单价
        enriched.get("warehouse_sku") or "",         # K SKU
        FIXED_FIELDS["L"],                           # L 商品价格税
        FIXED_FIELDS["M"],                           # M 税种
        FIXED_FIELDS["N"],                           # N 税率%
        FIXED_FIELDS["O"],                           # O 折扣税金额
        FIXED_FIELDS["P"],                           # P 折扣金额
        FIXED_FIELDS["Q"],                           # Q 运费
        str(enriched.get("warehouse_id") or ""),     # R 订单发货仓ID
        str(enriched.get("shipping_channel_id") or ""),  # S 仓配物流渠道ID
        label_url or "",                             # T 提货面单
        tracking_number or "",                       # U 订单物流运单号
        "",                                          # V 收货人姓
        FIXED_FIELDS["W"],                           # W 收货邮箱
        FIXED_FIELDS["X"],                           # X 国家
        FIXED_FIELDS["Y"],                           # Y 国家简码
        enriched.get("recipient_name") or "",        # Z 收货人名
        enriched.get("recipient_street") or "",      # AA 详细地址1
        enriched.get("recipient_street2") or "",     # AB 详细地址2
        enriched.get("recipient_city") or "",        # AC 城市
        enriched.get("recipient_state") or "",       # AD 州省
        enriched.get("recipient_zip") or "",         # AE 邮编
        enriched.get("recipient_phone") or "",       # AF 联系电话
        FIXED_FIELDS["AG"],                          # AG 用户邮箱
        "",                                          # AH 折扣率
        "",                                          # AI 商品折扣含税单价
    ]
    # tab-separated; any tab/newline in a value gets replaced
    cleaned = [str(c).replace("\t", " ").replace("\n", " ").replace("\r", " ") for c in cols]
    return "\t".join(cleaned)


# -- Persist + write back -----------------------------------------------------

def _write_back_order_tracking(txn_id: str, tracking: str) -> None:
    """Best-effort: stamp Tracking + Status='SHIPPED' on the matching order
    record. We try each known order table since AutoWeb has tables for
    macy/bestbuy/walmart/lowes but no dedicated HD table; on Teapplix the
    txn_id has its own prefix scheme. If no row matches in any table we
    just log and move on - the source-of-truth is hd_label_records itself."""
    candidates = ["macyorder", "bestbuyorder", "walmartorder", "lowesorder"]
    conn = DBManager.get_connection()
    try:
        updated_any = False
        with conn.cursor() as cur:
            for tbl in candidates:
                try:
                    cur.execute(
                        f"UPDATE {tbl} SET Tracking=%s, Status='SHIPPED' "
                        f"WHERE Order_ID=%s AND (Tracking IS NULL OR Tracking='')",
                        (tracking, txn_id),
                    )
                    if cur.rowcount:
                        updated_any = True
                        log.info("Tracking written to %s for %s", tbl, txn_id)
                        break
                except Exception as e:
                    log.debug("update %s skipped: %s", tbl, e)
        conn.commit()
        if not updated_any:
            log.info("No order-table row matched txn %s (HD orders may not be mirrored)", txn_id)
    except Exception:
        conn.rollback()
    finally:
        conn.close()


def _save_record(rec: Dict[str, Any]) -> None:
    """Upsert into hd_label_records by txn_id PK."""
    sql = """
    INSERT INTO hd_label_records (
        txn_id, store_key, shop_sku, warehouse_sku, profile_id, warehouse_id,
        shipping_channel_id, method, weight_lb, length_in, width_in, depth_in,
        quantity, order_total, recipient_name, recipient_state, recipient_zip,
        tracking_number, label_url, postage, provider, status,
        error_code, error_msg, request_json, response_json, excel_row_tsv
    ) VALUES (
        %(txn_id)s, %(store_key)s, %(shop_sku)s, %(warehouse_sku)s, %(profile_id)s,
        %(warehouse_id)s, %(shipping_channel_id)s, %(method)s, %(weight_lb)s,
        %(length_in)s, %(width_in)s, %(depth_in)s, %(quantity)s, %(order_total)s,
        %(recipient_name)s, %(recipient_state)s, %(recipient_zip)s,
        %(tracking_number)s, %(label_url)s, %(postage)s, %(provider)s, %(status)s,
        %(error_code)s, %(error_msg)s, %(request_json)s, %(response_json)s,
        %(excel_row_tsv)s
    )
    ON DUPLICATE KEY UPDATE
        store_key=VALUES(store_key), shop_sku=VALUES(shop_sku),
        warehouse_sku=VALUES(warehouse_sku), profile_id=VALUES(profile_id),
        warehouse_id=VALUES(warehouse_id), shipping_channel_id=VALUES(shipping_channel_id),
        method=VALUES(method), weight_lb=VALUES(weight_lb),
        length_in=VALUES(length_in), width_in=VALUES(width_in), depth_in=VALUES(depth_in),
        quantity=VALUES(quantity), order_total=VALUES(order_total),
        recipient_name=VALUES(recipient_name), recipient_state=VALUES(recipient_state),
        recipient_zip=VALUES(recipient_zip),
        tracking_number=VALUES(tracking_number), label_url=VALUES(label_url),
        postage=VALUES(postage), provider=VALUES(provider), status=VALUES(status),
        error_code=VALUES(error_code), error_msg=VALUES(error_msg),
        request_json=VALUES(request_json), response_json=VALUES(response_json),
        excel_row_tsv=VALUES(excel_row_tsv)
    """
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, rec)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# -- Public actions -----------------------------------------------------------

def purchase_one(txn_id: str) -> Dict[str, Any]:
    """Top-level: identify Vevor, choose warehouse, fetch dims, call Teapplix,
    persist + write back. Returns a JSON-serialisable result dict for the UI.

    Idempotent on hd_label_records — if there's already a success record we
    refuse to re-purchase (avoids double-charging postage)."""
    existing = get_existing_record(txn_id)
    if existing and existing.get("status") == "success":
        return {
            "ok": False, "txn_id": txn_id, "reason": "already_shipped",
            "tracking_number": existing.get("tracking_number"),
            "label_url": existing.get("label_url"),
            "excel_row_tsv": existing.get("excel_row_tsv"),
        }

    order = tp.get_order(txn_id)
    if not order:
        return {"ok": False, "txn_id": txn_id, "reason": "order_not_found"}

    enriched = enrich_orders([order])[0]
    if not enriched["is_vevor"]:
        return {"ok": False, "txn_id": txn_id, "reason": "not_vevor",
                "warnings": enriched["warnings"]}
    if not enriched["has_dims"]:
        return {"ok": False, "txn_id": txn_id, "reason": "missing_dims",
                "warnings": enriched["warnings"]}
    if not enriched["profile_id"]:
        return {"ok": False, "txn_id": txn_id, "reason": "no_profile",
                "warnings": enriched["warnings"]}

    res = tp.purchase_label(
        txn_id=txn_id,
        profile_id=enriched["profile_id"],
        method=DEFAULT_METHOD,
        weight_lb=enriched["weight_lb_ceil"],
        length_in=enriched["length_in_ceil"],
        width_in=enriched["width_in_ceil"],
        depth_in=enriched["depth_in_ceil"],
        quantity=enriched["quantity"],
        line_number=1,
    )
    body = res.get("body") or {}
    status_code = res.get("status")
    if status_code == 200 and (body.get("Status") == "Purchased"):
        tracking = tp.extract_tracking(body)
        label_url = tp.extract_label_url(body)
        postage = tp.extract_postage(body)
        tsv = build_excel_tsv(enriched, tracking_number=tracking, label_url=label_url)
        record = {
            "txn_id": txn_id,
            "store_key": enriched["store_key"],
            "shop_sku": enriched["shop_sku"],
            "warehouse_sku": enriched["warehouse_sku"],
            "profile_id": enriched["profile_id"],
            "warehouse_id": enriched["warehouse_id"],
            "shipping_channel_id": enriched["shipping_channel_id"],
            "method": DEFAULT_METHOD,
            "weight_lb": enriched["weight_lb_ceil"],
            "length_in": enriched["length_in_ceil"],
            "width_in": enriched["width_in_ceil"],
            "depth_in": enriched["depth_in_ceil"],
            "quantity": enriched["quantity"],
            "order_total": enriched["order_total"],
            "recipient_name": enriched["recipient_name"],
            "recipient_state": enriched["recipient_state"],
            "recipient_zip": enriched["recipient_zip"],
            "tracking_number": tracking,
            "label_url": label_url,
            "postage": postage,
            "provider": body.get("Provider"),
            "status": "success",
            "error_code": None,
            "error_msg": None,
            "request_json": json.dumps(res.get("request"), ensure_ascii=False),
            "response_json": json.dumps(body, ensure_ascii=False),
            "excel_row_tsv": tsv,
        }
        _save_record(record)
        if tracking:
            _write_back_order_tracking(txn_id, tracking)
        return {
            "ok": True, "txn_id": txn_id,
            "tracking_number": tracking, "label_url": label_url,
            "postage": postage, "profile_id": enriched["profile_id"],
            "warehouse_id": enriched["warehouse_id"], "method": DEFAULT_METHOD,
            "excel_row_tsv": tsv,
        }
    # failure path
    code, msg = tp.extract_error(body)
    record = {
        "txn_id": txn_id,
        "store_key": enriched["store_key"],
        "shop_sku": enriched["shop_sku"],
        "warehouse_sku": enriched["warehouse_sku"],
        "profile_id": enriched["profile_id"],
        "warehouse_id": enriched["warehouse_id"],
        "shipping_channel_id": enriched["shipping_channel_id"],
        "method": DEFAULT_METHOD,
        "weight_lb": enriched["weight_lb_ceil"],
        "length_in": enriched["length_in_ceil"],
        "width_in": enriched["width_in_ceil"],
        "depth_in": enriched["depth_in_ceil"],
        "quantity": enriched["quantity"],
        "order_total": enriched["order_total"],
        "recipient_name": enriched["recipient_name"],
        "recipient_state": enriched["recipient_state"],
        "recipient_zip": enriched["recipient_zip"],
        "tracking_number": None,
        "label_url": None,
        "postage": None,
        "provider": None,
        "status": "failed",
        "error_code": code,
        "error_msg": msg,
        "request_json": json.dumps(res.get("request"), ensure_ascii=False),
        "response_json": json.dumps(body, ensure_ascii=False),
        "excel_row_tsv": None,
    }
    _save_record(record)
    return {
        "ok": False, "txn_id": txn_id,
        "reason": "teapplix_error",
        "http_status": status_code,
        "error_code": code, "error_msg": msg,
    }


def purchase_many(txn_ids: List[str]) -> Dict[str, Any]:
    """Sequential: Teapplix has no batch endpoint and label purchases hit
    real money. We cap at BATCH_LIMIT and run serially so the user can see
    partial progress and we can rollback per-row."""
    txn_ids = list(dict.fromkeys([t for t in txn_ids if t]))[:BATCH_LIMIT]
    results = []
    for txn in txn_ids:
        try:
            results.append(purchase_one(txn))
        except Exception as e:
            log.exception("purchase_one crashed for %s", txn)
            results.append({"ok": False, "txn_id": txn, "reason": "exception",
                            "error_msg": str(e)})
    success = [r for r in results if r.get("ok")]
    fail = [r for r in results if not r.get("ok")]
    combined_tsv = "\n".join(r.get("excel_row_tsv") or "" for r in success
                             if r.get("excel_row_tsv"))
    return {
        "ok": True,
        "total": len(results),
        "success_count": len(success),
        "fail_count": len(fail),
        "results": results,
        "combined_excel_tsv": combined_tsv,
    }


def cancel_one(txn_id: str, reason: Optional[str] = None,
               force: bool = False) -> Dict[str, Any]:
    """Cancel a previously purchased label. If Teapplix reports the batch is
    already gone (Code 25004 'Batch is not found'), treat it as a successful
    sync — the label was likely cancelled directly in the Teapplix UI."""
    existing = get_existing_record(txn_id)
    if not existing or existing.get("status") != "success":
        return {"ok": False, "txn_id": txn_id, "reason": "no_active_label"}

    res = tp.cancel_label(txn_id, force=force)
    body = res.get("body") or {}
    status_code = res.get("status")
    code, msg = tp.extract_error(body)

    is_cancelled_ok = (status_code == 200 and body.get("Status") == "Cancelled")
    is_already_gone = (code == 25004)  # "Batch is not found" - cancelled in TP UI

    if is_cancelled_ok or is_already_gone:
        note = (reason or "") + (
            "" if is_cancelled_ok else " [auto-synced: already cancelled in Teapplix]"
        )
        _exec(
            "UPDATE hd_label_records SET status='cancelled', "
            "cancelled_at=NOW(), cancel_reason=%s, "
            "response_json=%s "
            "WHERE txn_id=%s",
            (note.strip(), json.dumps(body, ensure_ascii=False), txn_id),
        )
        return {
            "ok": True, "txn_id": txn_id, "status": "cancelled",
            "synced_from_teapplix": is_already_gone,
        }
    return {
        "ok": False, "txn_id": txn_id, "reason": "cancel_failed",
        "http_status": status_code, "error_code": code, "error_msg": msg,
    }
