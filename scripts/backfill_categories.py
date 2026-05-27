"""One-shot category backfill for offerprice_listing via OF21.

OF52 export does not return category_label/code, so cron-synced rows land
with NULL. This script picks up every active offer with category IS NULL
for a given store and fetches its category via OF21 (one call per SKU,
~2s each, no Mirakl rate cap). Reentrant: only NULL rows are touched, so
interrupting and re-running just continues where it left off.

Usage:
    ./venv/bin/python scripts/backfill_categories.py --store macy_kuyotq
    ./venv/bin/python scripts/backfill_categories.py --store lowes_yasonic --limit 50  # test run

The --limit flag is mainly for smoke tests; omit it to backfill everything.
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
    backfill_categories_via_of21,
)
from config import config as _cfg_map


def _pick_skus_to_backfill(store_key: str, limit: int | None) -> list[str]:
    """Return shop_skus that still need a category. Active offers only."""
    cfg = _cfg_map[os.environ.get("FLASK_CONFIG", "production")]
    conn = pymysql.connect(
        host=cfg.DB_HOST, user=cfg.DB_USER, password=cfg.DB_PASS,
        database="order_system", cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        store_cfg = STORE_CONFIGS[store_key]
        sql = """
            SELECT shop_sku FROM offerprice_listing
             WHERE platform=%s AND shop_name=%s
               AND active=1
               AND (category IS NULL OR category = '')
             ORDER BY shop_sku
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

    skus = _pick_skus_to_backfill(store_key, args.limit or None)
    print(f"[backfill][{store_key}] {len(skus)} SKUs need category")
    if not skus:
        print(json.dumps({"success": True, "store_key": store_key, "attempted": 0, "msg": "nothing to do"}))
        return 0

    eta_min = len(skus) * args.sleep / 60
    print(f"[backfill][{store_key}] estimated ETA at {args.sleep}s/call: {eta_min:.1f} min")

    config_name = os.environ.get("FLASK_CONFIG", "production")
    app = create_app(config_name)
    with app.app_context():
        result = backfill_categories_via_of21(
            store_key, skus, sleep_seconds=args.sleep,
        )

    result["store_key"] = store_key
    result["success"] = True
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
