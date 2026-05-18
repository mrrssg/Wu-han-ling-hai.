"""One-shot: provision lowes_yasonic store (idempotent).

Creates lowes_yasonic_transaction_logs (LIKE lowes_autool) and inserts a
shop_configs row. Run as admin on the server:

    cd /var/www/autoweb/AutoWeb
    FLASK_CONFIG=production ./venv/bin/python scripts/setup_lowes_yasonic.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app
from app.models.db_manager import DBManager


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `order_system`.`lowes_yasonic_transaction_logs`
  LIKE `order_system`.`lowes_autool_transaction_logs`
"""

INSERT_SHOP_SQL = """
INSERT INTO `order_system`.`shop_configs`
  (platform, shop_name, proxy_ip, proxy_port, proxy_user, proxy_pass, user_agent, is_active)
SELECT 'lowes-yasonic', 'yasonic', '208.214.165.240', '50100',
       'mrrssgm22', 'YDrXX3QIdX',
       'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0',
       1
WHERE NOT EXISTS (
    SELECT 1 FROM `order_system`.`shop_configs`
    WHERE platform='lowes-yasonic' AND shop_name='yasonic'
)
"""


def main() -> None:
    app = create_app("production")
    with app.app_context():
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(CREATE_TABLE_SQL)
                print("[OK] CREATE TABLE lowes_yasonic_transaction_logs")

                cur.execute(INSERT_SHOP_SQL)
                print(f"[OK] INSERT shop_configs (rows affected: {cur.rowcount})")

                cur.execute(
                    """
                    SELECT id, platform, shop_name, proxy_ip, proxy_port, is_active
                    FROM `order_system`.`shop_configs`
                    WHERE platform='lowes-yasonic' AND shop_name='yasonic'
                    """
                )
                row = cur.fetchone()
                print("\n=== shop_configs row ===")
                print(row)

                cur.execute(
                    """
                    SELECT COUNT(*) AS col_cnt
                    FROM information_schema.columns
                    WHERE table_schema='order_system'
                      AND table_name='lowes_yasonic_transaction_logs'
                    """
                )
                cols = cur.fetchone()
                print(f"\n=== transaction_logs columns: {cols} ===")
            conn.commit()
        finally:
            conn.close()


if __name__ == "__main__":
    main()
