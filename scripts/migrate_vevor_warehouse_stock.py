"""
Idempotent schema migration: add per-warehouse stock columns to
autooperate.newestdropship_vevor.

Adds:
  - Stock_W10  INT NOT NULL DEFAULT 0  (美东NJ-谷仓 美国新泽西仓(10))
  - Stock_W432 INT NOT NULL DEFAULT 0  (美西CA-谷仓 美国洛杉矶9仓(432))

Safe to run multiple times - uses information_schema checks.

Usage (server, as admin user):
    PYTHONPATH=/var/www/autoweb/AutoWeb FLASK_CONFIG=production \\
        ./venv/bin/python scripts/migrate_vevor_warehouse_stock.py
"""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app import create_app
from app.models.db_manager import DBManager


NEW_COLUMNS = [
    ("Stock_W10",  "INT NOT NULL DEFAULT 0 COMMENT '美东NJ-谷仓 美国新泽西仓(10)'"),
    ("Stock_W432", "INT NOT NULL DEFAULT 0 COMMENT '美西CA-谷仓 美国洛杉矶9仓(432)'"),
]


def _column_exists(cursor, schema, table, column):
    cursor.execute(
        """SELECT 1 FROM information_schema.columns
           WHERE table_schema=%s AND table_name=%s AND column_name=%s LIMIT 1""",
        (schema, table, column),
    )
    return cursor.fetchone() is not None


def main():
    app = create_app()
    with app.app_context():
        schema = app.config["DB_NAME"]
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cur:
                for col, ddl in NEW_COLUMNS:
                    if _column_exists(cur, schema, "newestdropship_vevor", col):
                        print(f"  column `{col}` already exists, skip")
                        continue
                    print(f"  adding column `{col}` {ddl}")
                    cur.execute(
                        f"ALTER TABLE `{schema}`.`newestdropship_vevor` "
                        f"ADD COLUMN `{col}` {ddl}"
                    )
            conn.commit()
            print("Done.")
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


if __name__ == "__main__":
    main()
