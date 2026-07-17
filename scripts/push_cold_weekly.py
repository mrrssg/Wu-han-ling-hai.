# -*- coding: utf-8 -*-
"""每周一自动推送零销量促活批（用户2026-07-17授权）。

范围铁律：只推 pricing_tier.tier='cold_12' 的候选（上架30天+零销量、降到12%档促活、
本来就没有销量可伤），每周最多 WEEKLY_CAP 个；12/15/18档等在售SKU仍然必须人工确认推送。

实现：不另写推送逻辑——直接经 test_client 调 web 的 /repricing/push-batch，
和人工点按钮走完全同一条管道（OF21全量重建 + push_price_guard校验只拦不改 +
OF24批推 + 逐SKU审计日志 + DB回写 + 飞书供应商价写回），夜里03:30回读验证自动覆盖。

前置安全闸：最新 plan run 必须是48小时内生成的（评档cron挂了就不推）。
"""
import sys
from datetime import datetime, timedelta

from app import create_app

STORE = "lowes_autool"
CHUNK = 50          # push-batch 单次上限（OF24_DEFAULT_BATCH_SIZE）
WEEKLY_CAP = 500    # 与 COLD_BATCH 对齐：每周一批


def main() -> int:
    app = create_app("production")
    with app.app_context():
        from app.models.db_manager import DBManager
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT run_id, MAX(triggered_at) AS t
                       FROM order_system.offer_price_change_log
                       WHERE run_id LIKE %s AND status='dry_run'
                       GROUP BY run_id ORDER BY t DESC LIMIT 1""",
                    (f"plan-{STORE}-%",))
                row = cur.fetchone()
                if not row:
                    print("no plan run found; abort")
                    return 1
                run_id, run_t = row["run_id"], row["t"]
                if run_t < datetime.now() - timedelta(hours=48):
                    print(f"latest plan run {run_id} is stale ({run_t}); abort")
                    return 1
                cur.execute(
                    """SELECT log.shop_sku
                       FROM order_system.offer_price_change_log log
                       JOIN order_system.pricing_tier t
                         ON t.store_key=%s AND t.shop_sku=log.shop_sku
                            AND t.tier='cold_12'
                       WHERE log.run_id=%s AND log.status='dry_run'
                         AND NOT EXISTS (
                           SELECT 1 FROM order_system.offer_price_change_log later
                           WHERE later.shop_sku=log.shop_sku
                             AND later.store_key=%s
                             AND later.triggered_at > log.triggered_at
                             AND later.status='success')
                       ORDER BY log.shop_sku LIMIT %s""",
                    (STORE, run_id, STORE, WEEKLY_CAP))
                skus = [r["shop_sku"] for r in cur.fetchall()]
        finally:
            conn.close()

        if not skus:
            print(f"[{run_id}] no cold_12 candidates to push; done")
            return 0
        print(f"[{run_id}] pushing {len(skus)} cold_12 SKUs, chunk={CHUNK}")

        client = app.test_client()
        with client.session_transaction() as s:
            s["logged_in"] = True

        pushed = rejected = failed = 0
        for i in range(0, len(skus), CHUNK):
            chunk = skus[i:i + CHUNK]
            resp = client.post(f"/repricing/push-batch?store={STORE}",
                               json={"shop_skus": chunk})
            data = resp.get_json(silent=True) or {}
            n_push = int(data.get("pushed") or 0)
            n_rej = len(data.get("rejections") or []) + len(data.get("of21_failures") or [])
            if data.get("success"):
                pushed += n_push
            else:
                failed += n_push
            rejected += n_rej
            print(f"  chunk {i // CHUNK + 1}: http={resp.status_code} "
                  f"success={data.get('success')} pushed={n_push} "
                  f"rejected={n_rej} import={data.get('import_id')} "
                  f"ip={data.get('ip_used')}")
            for r in (data.get("rejections") or [])[:5]:
                print(f"    reject: {r}")

        print(f"done: pushed={pushed} failed={failed} rejected_or_of21={rejected}")
        return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
