"""
One-shot migration: switch 4 active shop_configs to Brightdata proxy.

Steps:
  1. Backup current rows to instance/shop_configs_backup_<ts>.json
  2. UPDATE 4 active shops (Kuyotq/Wopet/autool/yasonic) to Brightdata
     - host  = brd.superproxy.io
     - port  = 33335
     - user  = brd-customer-hl_14404d60-zone-isp_proxy5-country-us-session-<shop>
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
    # (platform, shop_name, session_suffix)
    ("Macys-Kuyotq", "Kuyotq", "kuyotq"),
    ("Macys-Wopet", "Wopet", "wopet"),
    ("lowes-autool", "autool", "autool"),
    ("lowes-yasonic", "yasonic", "yasonic"),
]

BRD_HOST = "brd.superproxy.io"
BRD_PORT = "33335"
BRD_USER_TPL = "brd-customer-hl_14404d60-zone-isp_proxy5-country-us-session-{}"
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
        for platform, shop_name, session in SHOPS:
            user = BRD_USER_TPL.format(session)
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
            print(f"[update] {platform:18s} {shop_name:10s} -> session-{session}")
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
    print("\n[reachability check] hitting geo.brdtest.com via each shop's session")
    for _, _, session in SHOPS:
        user = quote(BRD_USER_TPL.format(session), safe="")
        pwd = quote(BRD_PASS, safe="")
        proxy_url = f"http://{user}:{pwd}@{BRD_HOST}:{BRD_PORT}"
        try:
            resp = requests.get(
                "https://geo.brdtest.com/welcome.txt?product=isp",
                proxies={"http": proxy_url, "https": proxy_url},
                timeout=20,
            )
            exit_ip = resp.headers.get("x-brd-ip", "?")
            print(f"  session-{session:8s} HTTP {resp.status_code}  exit_ip={exit_ip}")
        except Exception as exc:
            print(f"  session-{session:8s} ERROR {exc}")


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
