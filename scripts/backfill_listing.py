"""One-shot mirakl_listing backfill via OF21.

For one store, find all active SKUs in offerprice_listing that do NOT have
a corresponding row in mirakl_listing yet (or that have not been refreshed
recently), and pull their full OF21 payload to upsert. Each OF21 call also
updates offerprice_listing.category - same code path used by the OF52 sync
end-of-run hook, so new SKUs are auto-populated going forward.

Reentrant: only SKUs missing in mirakl_listing get touched, so interrupting
and re-running just continues where it left off.

Usage:
    ./venv/bin/python scripts/backfill_listing.py --store macy_kuyotq
    ./venv/bin/python scripts/backfill_listing.py --store lowes_yasonic --limit 50    # test
"""
import argparse
import json
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pymysql

from app import create_app
from app.services.offer_listing_sync_service import (
    STORE_CONFIGS,
    backfill_listing_via_of21,
    _ensure_listing_schema,
)
from config import config as _cfg_map


def _pick_skus_to_backfill(store_key: str, limit: int | None) -> list[str]:
    """Return shop_skus that don't yet have a mirakl_listing row. Active only."""
    cfg = _cfg_map[os.environ.get("FLASK_CONFIG", "production")]
    conn = pymysql.connect(
        host=cfg.DB_HOST, user=cfg.DB_USER, password=cfg.DB_PASS,
        database="order_system", cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        store_cfg = STORE_CONFIGS[store_key]
        sql = """
            SELECT o.shop_sku
              FROM offerprice_listing o
         LEFT JOIN mirakl_listing l
                ON o.platform = l.platform
               AND o.shop_name = l.shop_name
               AND o.shop_sku = l.shop_sku
             WHERE o.platform=%s AND o.shop_name=%s
               AND o.active=1
               AND l.shop_sku IS NULL
             ORDER BY o.shop_sku
        """
        params = [store_cfg["platform"], store_cfg["shop_name"]]
        if limit and limit > 0:
            sql += " LIMIT %s"
            params.append(limit)
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return [r["shop_sku"] for r in cur.fetchall() if r.get("shop_sku")]
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True,
                        help="store_key: macy_kuyotq | macy_wopet | lowes_autool | lowes_yasonic")
    parser.add_argument("--limit", type=int, default=0,
                        help="cap how many SKUs to process this run (0 = all)")
    parser.add_argument("--sleep", type=float, default=2.0,
                        help="seconds between OF21 calls (default 2.0)")
    args = parser.parse_args()

    store_key = args.store.strip().lower()
    if store_key not in STORE_CONFIGS:
        print(json.dumps({
            "success": False,
            "msg": f"invalid store: {store_key}",
            "supported": list(STORE_CONFIGS.keys()),
        }, ensure_ascii=False))
        return 1

    # ensure table exists before counting
    config_name = os.environ.get("FLASK_CONFIG", "production")
    app = create_app(config_name)
    with app.app_context():
        _ensure_listing_schema()

    skus = _pick_skus_to_backfill(store_key, args.limit or None)
    print(f"[backfill_listing][{store_key}] {len(skus)} SKUs need a mirakl_listing row")
    if not skus:
        print(json.dumps({"success": True, "store_key": store_key, "attempted": 0, "msg": "nothing to do"}))
        return 0

    eta_min = len(skus) * args.sleep / 60
    print(f"[backfill_listing][{store_key}] estimated ETA at {args.sleep}s/call: {eta_min:.1f} min")

    with app.app_context():
        result = backfill_listing_via_of21(
            store_key, skus, sleep_seconds=args.sleep,
        )

    result["store_key"] = store_key
    result["success"] = True
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
