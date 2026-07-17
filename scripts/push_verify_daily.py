# -*- coding: utf-8 -*-
"""推价回读验证（2026-07-16错价事故第三道防线）。

夜间offer同步(02:30)之后跑：把近48小时推送成功的价格与数据库里Mirakl同步回来的
真实价格/状态逐一比对——价格没生效、被平台改动、或offer被停用(INACTIVE)的，
写 issue_log(price_push_mismatch) → 首页今日待办自动报警。

Usage:
    PYTHONPATH=/var/www/autoweb/AutoWeb FLASK_CONFIG=production \
        ./venv/bin/python scripts/push_verify_daily.py
"""
import json
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app import create_app

PRICE_TOL = 0.02


def main() -> int:
    app = create_app(os.environ.get("FLASK_CONFIG", "production"))
    with app.app_context():
        from app.models.db_manager import DBManager
        from app.services.repricing_stores import REPRICING_STORES
        conn = DBManager.get_connection()
        found = []
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT store_key, shop_sku, new_origin_price, new_discount_price,
                           triggered_at
                    FROM order_system.offer_price_change_log
                    WHERE status='success'
                      AND triggered_at >= DATE_SUB(NOW(), INTERVAL 48 HOUR)""")
                pushes = cur.fetchall()
                for p in pushes:
                    scfg = REPRICING_STORES.get(p["store_key"])
                    if not scfg:
                        continue
                    cur.execute("""
                        SELECT origin_price, discount_price, active, updated_at
                        FROM order_system.offerprice_listing
                        WHERE platform=%s AND shop_name=%s AND shop_sku=%s""",
                        (scfg["platform"], scfg["shop_name"], p["shop_sku"]))
                    o = cur.fetchone()
                    problems = []
                    if not o:
                        problems.append("offer在库里不存在")
                    else:
                        if o["updated_at"] and o["updated_at"] <= p["triggered_at"]:
                            continue   # 同步还没跑到推送之后，下次再验
                        if not o["active"]:
                            problems.append("推价后offer变为INACTIVE(疑似被平台停用)")
                        po = float(p["new_origin_price"] or 0)
                        oo = float(o["origin_price"] or 0)
                        if po and abs(oo - po) > PRICE_TOL:
                            problems.append(f"原价未生效: 推了{po}实际{oo}")
                        pd_ = float(p["new_discount_price"] or 0)
                        od = float(o["discount_price"] or 0)
                        if pd_ and od and abs(od - pd_) > PRICE_TOL:
                            problems.append(f"折扣价未生效: 推了{pd_}实际{od}")
                    if problems:
                        entity = f"{p['shop_sku']}@{p['store_key']}"
                        cur.execute("""
                            SELECT id FROM order_system.issue_log
                            WHERE issue_type='price_push_mismatch' AND entity=%s
                              AND status='open'""", (entity,))
                        if not cur.fetchone():
                            cur.execute("""
                                INSERT INTO order_system.issue_log
                                    (detected_date, issue_type, entity, severity,
                                     impact_usd, evidence, suggestion, status)
                                VALUES (CURDATE(), 'price_push_mismatch', %s, 'high', 0,
                                        %s, '去Mirakl后台核对价格与offer状态；确认后手动关闭本条', 'open')""",
                                (entity, "；".join(problems)))
                        found.append({"entity": entity, "problems": problems})

                # ---- Excel整体导出的回读对账（2026-07-17用户拍板B）----
                # 导出页点过"已上传"的run：夜间同步后逐行比对 Mirakl真实价 vs 文件目标价。
                # 还是上传前老价的行不按错误报（用户可能删行分批传），只计数进日志；
                # 价格变了但不等于目标价的才开 issue。验完标 verified，不重复验。
                exports_checked = []
                cur.execute("""
                    SELECT id, target AS run_id, store AS store_key, created_at
                    FROM order_system.action_log
                    WHERE action_type='full_export_uploaded' AND status='executed'""")
                for m in cur.fetchall():
                    scfg = REPRICING_STORES.get(m["store_key"])
                    if not scfg:
                        continue
                    cur.execute("""
                        SELECT MAX(updated_at) AS t FROM order_system.offerprice_listing
                        WHERE platform=%s AND shop_name=%s""",
                        (scfg["platform"], scfg["shop_name"]))
                    sync_t = (cur.fetchone() or {}).get("t")
                    if not sync_t or sync_t <= m["created_at"]:
                        continue   # 标记之后同步还没跑，下次再验
                    cur.execute("""
                        SELECT shop_sku, old_origin_price, new_origin_price, new_discount_price
                        FROM order_system.offer_price_change_log
                        WHERE run_id=%s AND run_type='full_export' AND status='dry_run'""",
                        (m["run_id"],))
                    intents = cur.fetchall()
                    applied = untouched = bad = 0
                    for p in intents:
                        cur.execute("""
                            SELECT origin_price, discount_price, active
                            FROM order_system.offerprice_listing
                            WHERE platform=%s AND shop_name=%s AND shop_sku=%s""",
                            (scfg["platform"], scfg["shop_name"], p["shop_sku"]))
                        o = cur.fetchone()
                        if not o:
                            continue
                        po = float(p["new_origin_price"] or 0)
                        oo = float(o["origin_price"] or 0)
                        old = float(p["old_origin_price"] or 0)
                        if po and abs(oo - po) <= PRICE_TOL:
                            applied += 1
                            continue
                        if old and abs(oo - old) <= PRICE_TOL:
                            untouched += 1   # 还是上传前的老价——大概率这行没传，不报错
                            continue
                        bad += 1
                        problems = [f"Excel上传对账: 目标原价{po}，上传前{old}，现在{oo}"]
                        if not o["active"]:
                            problems.append("offer已INACTIVE")
                        entity = f"{p['shop_sku']}@{m['store_key']}"
                        cur.execute("""
                            SELECT id FROM order_system.issue_log
                            WHERE issue_type='price_push_mismatch' AND entity=%s
                              AND status='open'""", (entity,))
                        if not cur.fetchone():
                            cur.execute("""
                                INSERT INTO order_system.issue_log
                                    (detected_date, issue_type, entity, severity,
                                     impact_usd, evidence, suggestion, status)
                                VALUES (CURDATE(), 'price_push_mismatch', %s, 'high', 0,
                                        %s, '去Mirakl后台核对该SKU价格；或在待改价页重推；确认后关闭本条', 'open')""",
                                (entity, "；".join(problems)))
                        found.append({"entity": entity, "problems": problems})
                    metrics = {"rows": len(intents), "applied": applied,
                               "untouched": untouched, "mismatch": bad}
                    cur.execute("""
                        UPDATE order_system.action_log
                        SET status='verified', after_metrics=%s WHERE id=%s""",
                        (json.dumps(metrics, ensure_ascii=False), m["id"]))
                    exports_checked.append({"run_id": m["run_id"], **metrics})
            conn.commit()
        finally:
            conn.close()
        result = {"checked": len(pushes), "mismatches": len(found),
                  "exports_checked": exports_checked,
                  "detail": found, "success": True}
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
