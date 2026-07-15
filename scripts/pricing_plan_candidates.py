# -*- coding: utf-8 -*-
"""分档定价→待改价候选生成（dry_run）。只生成候选，推送在候选页人工确认。

Usage:
    PYTHONPATH=/var/www/autoweb/AutoWeb FLASK_CONFIG=production \
        ./venv/bin/python scripts/pricing_plan_candidates.py --store lowes_autool
"""
import argparse
import json
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app import create_app


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", default="lowes_autool")
    args = parser.parse_args()
    app = create_app(os.environ.get("FLASK_CONFIG", "production"))
    with app.app_context():
        from app.services.pricing_plan_service import generate_plan_candidates
        try:
            result = generate_plan_candidates(args.store)
            result["success"] = True
        except Exception as exc:
            import traceback
            traceback.print_exc()
            result = {"success": False, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
