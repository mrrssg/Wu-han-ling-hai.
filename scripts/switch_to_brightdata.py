"""
One-shot migration: switch 4 active shop_configs to Brightdata proxy.

Each shop is HARD-PINNED to one of the 4 static ISP IPs in the
isp_proxy5 zone. Session-based sticky was tried first but the zone
only has 4 IPs total and sessions hash into them, causing collisions
(two shops would share the same exit IP). Pinning via `-ip-X.X.X.X`
in the proxy username guarantees a 1:1 shop->IP mapping.

Steps:
  1. Backup current rows to instance/shop_configs_backup_<ts>.json
  2. UPDATE 4 active shops (Kuyotq/Wopet/autool/yasonic) to Brightdata
     - host  = brd.superproxy.io
     - port  = 33335
     - user  = brd-customer-hl_14404d60-zone-isp_proxy5-ip-<pinned_ip>
     - pass  = f73vfek34d52
  3. Print final state and reachability check via the new proxy.

BestBuy-Delphi is left untouched (deprecated store).

Usage (server, as admin):
    PYTHONPATH=/var/www/autoweb/AutoWeb FLASK_CONFIG=production \
        ./venv/bin/python scripts/switch_to_brightdata.py
"""
import json
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import pymysql
import requests

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import Config  # noqa: E402


SHOPS = [
    # (platform, shop_name, pinned_exit_ip)
    ("Macys-Kuyotq", "Kuyotq", "109.203.161.49"),
    ("Macys-Wopet", "Wopet", "72.56.175.171"),
    ("lowes-autool", "autool", "31.105.174.42"),
    ("lowes-yasonic", "yasonic", "31.98.211.161"),
]

BRD_HOST = "brd.superproxy.io"
BRD_PORT = "33335"
BRD_USER_TPL = "brd-customer-hl_14404d60-zone-isp_proxy5-ip-{}"
BRD_PASS = "f73vfek34d52"


def _connect():
    cfg = Config()
    return pymysql.connect(
        host=cfg.DB_HOST,
        user=cfg.DB_USER,
        password=cfg.DB_PASS,
        database="order_system",
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def _backup(conn) -> Path:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM shop_configs ORDER BY platform, shop_name"
        )
        rows = cur.fetchall()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = _PROJECT_ROOT / "instance"
    backup_dir.mkdir(exist_ok=True)
    backup_path = backup_dir / f"shop_configs_backup_{ts}.json"
    backup_path.write_text(
        json.dumps(rows, default=str, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[backup] saved {len(rows)} rows -> {backup_path}")
    return backup_path


def _update_shops(conn) -> None:
    with conn.cursor() as cur:
        for platform, shop_name, pinned_ip in SHOPS:
            user = BRD_USER_TPL.format(pinned_ip)
            affected = cur.execute(
                """
                UPDATE shop_configs
                   SET proxy_ip = %s,
                       proxy_port = %s,
                       proxy_user = %s,
                       proxy_pass = %s
                 WHERE platform = %s AND shop_name = %s
                """,
                (BRD_HOST, BRD_PORT, user, BRD_PASS, platform, shop_name),
            )
            if affected != 1:
                raise RuntimeError(
                    f"[update] expected 1 row for {platform}/{shop_name}, "
                    f"got {affected} -- ABORTING"
                )
            print(f"[update] {platform:18s} {shop_name:10s} -> pinned {pinned_ip}")
    conn.commit()
    print("[commit] all 4 shops updated")


def _print_final_state(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT platform, shop_name, proxy_ip, proxy_port, proxy_user, is_active
              FROM shop_configs
             ORDER BY platform, shop_name
            """
        )
        rows = cur.fetchall()
    print("\n[final state]")
    for r in rows:
        flag = "ACTIVE  " if r["is_active"] else "INACTIVE"
        print(
            f"  {flag} {r['platform']:18s} {r['shop_name']:10s} "
            f"{r['proxy_ip']}:{r['proxy_port']}  user={r['proxy_user']}"
        )


def _reachability_check() -> None:
    print("\n[reachability check] hitting api.ipify.org via each pinned IP")
    for platform, shop_name, pinned_ip in SHOPS:
        user = quote(BRD_USER_TPL.format(pinned_ip), safe="")
        pwd = quote(BRD_PASS, safe="")
        proxy_url = f"http://{user}:{pwd}@{BRD_HOST}:{BRD_PORT}"
        try:
            resp = requests.get(
                "https://api.ipify.org?format=json",
                proxies={"http": proxy_url, "https": proxy_url},
                timeout=20,
            )
            try:
                observed = resp.json().get("ip", "?")
            except Exception:
                observed = resp.text.strip()[:20]
            match = "OK" if observed == pinned_ip else "MISMATCH"
            print(
                f"  {platform:18s} {shop_name:10s} requested={pinned_ip:16s} "
                f"observed={observed:16s} {match}"
            )
        except Exception as exc:
            print(f"  {platform:18s} {shop_name:10s} ERROR {exc}")


def main() -> int:
    conn = _connect()
    try:
        _backup(conn)
        _update_shops(conn)
        _print_final_state(conn)
    finally:
        conn.close()
    _reachability_check()
    print("\n[done]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
