# -*- coding: utf-8 -*-
"""HD订单同步（2026-07-18用户拍板：先订单，退货不做）。

数据链：HD(CommerceHub) → Teapplix → 本表 hd_order_data。
接口经验（实测踩出来的）：
  * /OrderNotification 日期过滤参数是 PaymentDateStart / PaymentDateFinish
    （PaymentDateBegin/End 会被静默忽略然后返回全量第一页——坑）
  * Shipped=1 必须带日期范围否则400；Shipped=0 可不带
  * 分页：响应 Pagination{PageSize:100, PageNumber, TotalPages}，请求参数 PageNumber
    （带防呆：翻页后首单没变就停，防参数失效死循环）
"""
from datetime import datetime, timedelta
from typing import Any, Dict, List

from app.models.db_manager import DBManager


def _fetch_page(params: Dict[str, Any]) -> Dict[str, Any]:
    from app.services.teapplix_label_service import _get
    r = _get("/OrderNotification", params=params)
    if r["status"] != 200:
        raise RuntimeError(f"Teapplix OrderNotification {params} -> {r['status']}: "
                           f"{str(r['body'])[:200]}")
    return r["body"] if isinstance(r["body"], dict) else {}


def _fetch_all(base_params: Dict[str, Any]) -> List[Dict]:
    out: List[Dict] = []
    page, prev_first = 1, None
    while True:
        body = _fetch_page({**base_params, "PageNumber": page})
        orders = body.get("Orders") or []
        if not orders:
            break
        first = orders[0].get("TxnId")
        if first == prev_first:      # PageNumber没生效的防呆
            break
        prev_first = first
        out.extend(orders)
        pg = body.get("Pagination") or {}
        try:
            if page >= int(pg.get("TotalPages") or 1):
                break
        except (TypeError, ValueError):
            break
        page += 1
    return out


def _rows_from_order(o: Dict, shipped: int) -> List[tuple]:
    to = o.get("To") or {}
    det = o.get("OrderDetails") or {}
    tot = o.get("OrderTotals") or {}
    rows = []
    items = o.get("OrderItems") or [{}]
    for idx, it in enumerate(items, start=1):
        rows.append((
            o.get("TxnId"), int(it.get("LineNumber") or idx),
            (o.get("StoreKey") or "")[:32], (det.get("Invoice") or "")[:64],
            det.get("PaymentDate") or None, o.get("LastUpdateDate") or None,
            shipped,
            (to.get("Name") or "")[:120], (to.get("PhoneNumber") or "")[:40],
            (to.get("Email") or "")[:160],
            (to.get("Street") or "")[:255], (to.get("Street2") or "")[:255],
            (to.get("City") or "")[:80], (to.get("State") or "")[:40],
            (to.get("ZipCode") or "")[:16],
            (str(it.get("Name")) if it.get("Name") is not None else "")[:64],
            (it.get("Description") or "")[:400],
            int(it.get("Quantity") or 1), it.get("Amount"),
            tot.get("Total"),
            (det.get("Custom") or "")[:64], (det.get("Custom2") or "")[:64],
        ))
    return rows


UPSERT = """
INSERT INTO order_system.hd_order_data
    (txn_id, line_number, store_key, invoice, payment_date, last_update, shipped,
     buyer_name, phone, email, street, street2, city, state, zip,
     item_sku, item_desc, quantity, amount, order_total, warehouse_sku, custom2)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON DUPLICATE KEY UPDATE
    shipped=VALUES(shipped), last_update=VALUES(last_update),
    buyer_name=VALUES(buyer_name), phone=VALUES(phone), email=VALUES(email),
    street=VALUES(street), street2=VALUES(street2), city=VALUES(city),
    state=VALUES(state), zip=VALUES(zip), item_desc=VALUES(item_desc),
    quantity=VALUES(quantity), amount=VALUES(amount), order_total=VALUES(order_total),
    warehouse_sku=VALUES(warehouse_sku), custom2=VALUES(custom2)
"""


def sync_hd_orders(days: int = 3) -> Dict[str, Any]:
    """未发货全量 + 近days天已发货。tracking从打单记录表回填。"""
    unshipped = _fetch_all({"Shipped": 0})
    start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    finish = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    shipped = _fetch_all({"Shipped": 1,
                          "PaymentDateStart": start, "PaymentDateFinish": finish})

    rows: List[tuple] = []
    for o in unshipped:
        rows.extend(_rows_from_order(o, 0))
    for o in shipped:
        rows.extend(_rows_from_order(o, 1))

    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cur:
            for i in range(0, len(rows), 300):
                cur.executemany(UPSERT, rows[i:i + 300])
            # 之前standing为未发货、这次不在未发货清单里的老单 → 标已发货
            # （Teapplix那边发掉了；2022年的僵尸未发货单也靠这条慢慢收敛）
            if unshipped:
                ph = ",".join(["%s"] * len(unshipped))
                cur.execute(f"""UPDATE order_system.hd_order_data
                                SET shipped=1
                                WHERE shipped=0 AND txn_id NOT IN ({ph})""",
                            [o.get("TxnId") for o in unshipped])
            # 面单跟踪号回填
            cur.execute("""
                UPDATE order_system.hd_order_data h
                JOIN autooperate.hd_label_records l ON l.txn_id = h.txn_id
                SET h.tracking_number = l.tracking_number
                WHERE (h.tracking_number IS NULL OR h.tracking_number='')
                  AND l.tracking_number IS NOT NULL AND l.tracking_number<>''""")
        conn.commit()
    finally:
        conn.close()
    return {"unshipped": len(unshipped), "shipped_window": len(shipped),
            "rows_upserted": len(rows), "window_days": days}


def backfill_hd_orders(days: int = 120, step: int = 15) -> Dict[str, Any]:
    """历史回填：按step天一窗拉已发货订单。"""
    total = 0
    end = datetime.now() + timedelta(days=1)
    cursor = datetime.now() - timedelta(days=days)
    conn = DBManager.get_connection()
    try:
        while cursor < end:
            w_end = min(cursor + timedelta(days=step), end)
            orders = _fetch_all({"Shipped": 1,
                                 "PaymentDateStart": cursor.strftime("%Y-%m-%d"),
                                 "PaymentDateFinish": w_end.strftime("%Y-%m-%d")})
            rows: List[tuple] = []
            for o in orders:
                rows.extend(_rows_from_order(o, 1))
            with conn.cursor() as cur:
                for i in range(0, len(rows), 300):
                    cur.executemany(UPSERT, rows[i:i + 300])
            conn.commit()
            total += len(orders)
            print(f"  {cursor.date()} ~ {w_end.date()}: {len(orders)} orders")
            cursor = w_end
    finally:
        conn.close()
    return {"backfilled_orders": total, "days": days}
