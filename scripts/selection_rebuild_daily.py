# -*- coding: utf-8 -*-
"""选品候选池每日重建（cron，2026-07-27 上线）。

顺序：同步司顺 vevor_feed → 重建 Macy + Lowes-Autool + Lowes-Yasonic 候选池。
- vevor feed 下载 S3 xlsx 约 10 分钟(阿里云→us-west-2 国际带宽慢，但会跑完)；
  用 subprocess 隔离 + 20 分钟硬超时兜底，失败/超时不阻塞后续重建（用旧 feed 继续）。
- 页面「重建候选池」手动按钮不受影响，随时可点。
- 豪雅库存(newestdropship)由既有机制自动更新，这里不再单独同步。
"""
import os
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import create_app


def _sync_vevor_feed():
    """subprocess 跑 sync_vevor_feed.py，20 分钟兜底。失败只告警不抛。"""
    t = time.time()
    try:
        env = dict(os.environ)
        env.setdefault("FLASK_CONFIG", "production")
        env.setdefault("PYTHONPATH", str(_ROOT))
        rc = subprocess.run(
            [sys.executable, str(_ROOT / "scripts" / "sync_vevor_feed.py")],
            cwd=str(_ROOT), env=env, timeout=1200,
            capture_output=True, text=True)
        tail = (rc.stdout or "").strip().splitlines()[-1:] or [""]
        print(f"[feed] vevor_feed rc={rc.returncode} {time.time()-t:.0f}s {tail[0]}", flush=True)
    except subprocess.TimeoutExpired:
        print(f"[feed] vevor_feed 超时>1200s，用旧 feed 继续重建", flush=True)
    except Exception as exc:
        print(f"[feed] vevor_feed 失败(用旧 feed 继续): {exc}", flush=True)


def main() -> int:
    print(f"=== selection_rebuild_daily start ===", flush=True)
    _sync_vevor_feed()

    app = create_app(os.environ.get("FLASK_CONFIG", "production"))
    with app.app_context():
        from app.services.macy_selection_service import rebuild_pool as macy_rebuild
        from app.services.lowes_selection_service import rebuild_pool as lowes_rebuild
        jobs = [
            ("macy", macy_rebuild),
            ("lowes-autool", lambda: lowes_rebuild("autool")),
            ("lowes-yasonic", lambda: lowes_rebuild("yasonic")),
        ]
        for label, fn in jobs:
            t = time.time()
            try:
                r = fn()
                print(f"[rebuild] {label} {time.time()-t:.0f}s {r}", flush=True)
            except Exception as exc:
                print(f"[rebuild] {label} 失败: {exc}", flush=True)
    print("=== selection_rebuild_daily done ===", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
