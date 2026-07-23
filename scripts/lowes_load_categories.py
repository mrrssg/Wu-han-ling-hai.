# -*- coding: utf-8 -*-
"""Lowes 选品·灌全类目（2026-07-23）。
从飞书 Lowes全类目 tblqP5459R0Lq7ua 拉「完整路径」→ order_system.lowes_leaf_category。
注意：必须用 GET records 端点翻页（POST search 的 page_token 在此表不生效，会卡在前500条）。
完整路径字段本身就是完整叶子路径(L1/L2/L3[/L4])，直接用，不要拼 Level 列(很多行父级列为空)。
"""
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import create_app
from app.models.db_manager import DBManager

APP_ID = "cli_a940a2a1067adbd2"
SECRET = "i2mKLGVzUDmu4v0U9HYEYdMGc0ZvZAgU"
APP = "QEeubiXYGa83zXs3Zt8cSSJPnih"
TBL = "tblqP5459R0Lq7ua"


def _gt(v):
    if isinstance(v, str):
        return v
    if isinstance(v, list) and v and isinstance(v[0], dict):
        return "".join(x.get("text", "") for x in v)
    if isinstance(v, dict):
        return v.get("text") or v.get("name") or ""
    return str(v) if v is not None else ""


def _pull_paths():
    import requests
    tok = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": APP_ID, "app_secret": SECRET}, timeout=30
    ).json()["tenant_access_token"]
    H = {"Authorization": f"Bearer {tok}"}
    seen = {}
    pt = ""
    while True:
        url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP}"
               f"/tables/{TBL}/records?page_size=500" + (f"&page_token={pt}" if pt else ""))
        r = requests.get(url, headers=H, timeout=60).json()
        d = r.get("data") or {}
        for it in d.get("items") or []:
            full = _gt(it["fields"].get("完整路径 (productCat)")).strip().strip("/")
            if full and full not in seen:
                seen[full] = full.split("/")
        if not d.get("has_more"):
            break
        pt = d.get("page_token") or ""
        if not pt:
            break
    return seen


def main() -> int:
    app = create_app(os.environ.get("FLASK_CONFIG", "production"))
    with app.app_context():
        seen = _pull_paths()
        print("Lowes唯一类目路径:", len(seen))
        rows = []
        for full, seg in seen.items():
            l1 = seg[0]
            l2 = seg[1] if len(seg) > 1 else None
            l3 = seg[2] if len(seg) > 2 else None
            l4 = seg[3] if len(seg) > 3 else None
            rows.append((l1, l2, l3, l4, seg[-1], full[:400]))
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cur:
                for i in range(0, len(rows), 500):
                    chunk = rows[i:i + 500]
                    ph = ",".join(["(%s,%s,%s,%s,%s,%s,1)"] * len(chunk))
                    flat = [v for r in chunk for v in r]
                    cur.execute(f"""INSERT INTO order_system.lowes_leaf_category
                        (l1,l2,l3,l4,leaf,full_path,active) VALUES {ph}
                        ON DUPLICATE KEY UPDATE l1=VALUES(l1), leaf=VALUES(leaf), active=1""", flat)
            conn.commit()
            with conn.cursor() as cur:
                cur.execute("""SELECT COUNT(*) n, COUNT(DISTINCT l1) l1n
                               FROM order_system.lowes_leaf_category""")
                print("入库:", cur.fetchone())
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
