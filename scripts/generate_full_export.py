"""
CLI for generating the Part 2 full repricing xlsx (downloadable, NOT pushed).

Usage:
    PYTHONPATH=/var/www/autoweb/AutoWeb FLASK_CONFIG=production \
        ./venv/bin/python scripts/generate_full_export.py --store macy_kuyotq
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
from app.services.repricing_full_export_service import run_full_export


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", default="macy_kuyotq")
    parser.add_argument(
        "--output-dir",
        default="/var/www/autoweb/AutoWeb/instance/exports/repricing",
    )
    args = parser.parse_args()

    config_name = os.environ.get("FLASK_CONFIG", "production")
    app = create_app(config_name)
    with app.app_context():
        result = run_full_export(args.output_dir, store_key=args.store)

    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
