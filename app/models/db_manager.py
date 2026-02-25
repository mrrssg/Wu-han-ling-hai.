import pymysql
from flask import current_app
from datetime import datetime
import time


class DBManager:
    @staticmethod
    def get_connection():
        return pymysql.connect(
            host=current_app.config['DB_HOST'],
            user=current_app.config['DB_USER'],
            password=current_app.config['DB_PASS'],
            database=current_app.config['DB_NAME'],
            charset='utf8mb4',
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=10,
            read_timeout=120,
            write_timeout=120,
            autocommit=False,
        )

    @staticmethod
    def _get_max_sequence_for_table(table_name: str, prefix: str) -> int:
        if table_name not in {"macyorder", "bestbuyorder", "walmartorder"}:
            return 0

        conn = DBManager.get_connection()
        search_pattern = f"{prefix}%"
        max_seq = 0

        try:
            with conn.cursor() as cursor:
                query = f"SELECT CostwayOrder FROM {table_name} WHERE CostwayOrder LIKE %s"
                cursor.execute(query, (search_pattern,))
                results = cursor.fetchall()

                for row in results:
                    val = row.get("CostwayOrder") if isinstance(row, dict) else row[0]
                    if val:
                        try:
                            num = int(val.replace(prefix, ""))
                            if num > max_seq:
                                max_seq = num
                        except Exception:
                            continue
        except Exception as e:
            print(f"Sequence query failed: {e}")
        finally:
            conn.close()

        return max_seq

    @staticmethod
    def get_macy_max_sequence():
        today_str = datetime.now().strftime("%y%m%d")
        prefix = f"WHLH{today_str}-"
        return DBManager._get_max_sequence_for_table("macyorder", prefix)

    @staticmethod
    def get_bestbuy_max_sequence():
        today_str = datetime.now().strftime("%y%m%d")
        prefix = f"WHLHBB{today_str}-"
        return DBManager._get_max_sequence_for_table("bestbuyorder", prefix)

    @staticmethod
    def get_walmart_max_sequence():
        today_str = datetime.now().strftime("%y%m%d")
        prefix = f"WHLHWM{today_str}-"
        return DBManager._get_max_sequence_for_table("walmartorder", prefix)

    @staticmethod
    def insert_macy_orders(data_tuples):
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cursor:
                sql = """
                INSERT INTO macyorder (
                    `Order number`, `Order line no.`, `Date created`, `Shipping address first name`,
                    `Shipping address last name`, `Shipping address street 1`, `Shipping address street 2`,
                    `Shipping address country`, `Shipping address city`, `Shipping address state`,
                    `Shipping address zip`, `Quantity`, `Offer SKU`, `Unit price`, `CostwayOrder`, `Status`
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, '未发货')
                ON DUPLICATE KEY UPDATE
                    `Order line no.` = VALUES(`Order line no.`),
                    `Date created` = VALUES(`Date created`),
                    `Status` = `Status`
                """
                cursor.executemany(sql, data_tuples)
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    @staticmethod
    def search_orders(text):
        import re
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cursor:
                keywords = [k.strip() for k in re.split(r'[ ,;\n\t]+', text) if k.strip()]
                if not keywords:
                    return []

                regex_pattern = "|".join(keywords)

                query = """
                   SELECT 
                       'Walmart' AS Source, 
                       CASE
                           WHEN t1.SKU IS NOT NULL THEN '豪雅'
                           WHEN t2.SKU IS NOT NULL THEN '司顺'
                           WHEN t3.SKU IS NOT NULL THEN '大建'
                           WHEN t4.SKU IS NOT NULL THEN '致欧'
                           ELSE ''
                       END AS SupplierSource,
                       PO_Number AS OrderID, 
                       CostwayOrder,
                       Costway_SKU AS CostwaySKU,
                       CONCAT(First_Name, ' ', Last_Name) AS FullName,
                       walmartorder.SKU AS SKU, 
                       Qty, 
                       Order_Date AS Date, 
                       Status,
                       Tracking
                   FROM walmartorder
                   LEFT JOIN newestdropship t1 
                      ON t1.SKU COLLATE utf8mb4_unicode_ci = walmartorder.Costway_SKU COLLATE utf8mb4_unicode_ci
                   LEFT JOIN newestdropship_vevor t2 
                      ON t2.SKU COLLATE utf8mb4_unicode_ci = walmartorder.Costway_SKU COLLATE utf8mb4_unicode_ci
                   LEFT JOIN newestdropship_dajian t3 
                      ON t3.SKU COLLATE utf8mb4_unicode_ci = walmartorder.Costway_SKU COLLATE utf8mb4_unicode_ci
                   LEFT JOIN newestdropship_songmics t4 
                      ON t4.SKU COLLATE utf8mb4_unicode_ci = walmartorder.Costway_SKU COLLATE utf8mb4_unicode_ci
                   WHERE First_Name REGEXP %s 
                      OR Last_Name REGEXP %s 
                      OR PO_Number REGEXP %s 
                      OR walmartorder.SKU REGEXP %s 
                      OR walmartorder.Costway_SKU REGEXP %s 
                      OR CostwayOrder REGEXP %s

                   UNION ALL

                   SELECT 
                       'Macy' AS Source, 
                       CASE
                           WHEN t1.SKU IS NOT NULL THEN '豪雅'
                           WHEN t2.SKU IS NOT NULL THEN '司顺'
                           WHEN t3.SKU IS NOT NULL THEN '大建'
                           WHEN t4.SKU IS NOT NULL THEN '致欧'
                           ELSE ''
                       END AS SupplierSource,
                       `Order number` AS OrderID, 
                       CostwayOrder,
                       Costway_SKU AS CostwaySKU,
                       CONCAT(`Shipping address first name`, ' ', `Shipping address last name`) AS FullName,
                       macyorder.`Offer SKU` AS SKU, 
                       Quantity AS Qty, 
                       `Date created` AS Date, 
                       Status,
                       Tracking
                   FROM macyorder
                   LEFT JOIN newestdropship t1 
                      ON t1.SKU COLLATE utf8mb4_unicode_ci = macyorder.Costway_SKU COLLATE utf8mb4_unicode_ci
                   LEFT JOIN newestdropship_vevor t2 
                      ON t2.SKU COLLATE utf8mb4_unicode_ci = macyorder.Costway_SKU COLLATE utf8mb4_unicode_ci
                   LEFT JOIN newestdropship_dajian t3 
                      ON t3.SKU COLLATE utf8mb4_unicode_ci = macyorder.Costway_SKU COLLATE utf8mb4_unicode_ci
                   LEFT JOIN newestdropship_songmics t4 
                      ON t4.SKU COLLATE utf8mb4_unicode_ci = macyorder.Costway_SKU COLLATE utf8mb4_unicode_ci
                   WHERE `Shipping address first name` REGEXP %s 
                      OR `Shipping address last name` REGEXP %s 
                      OR `Order number` REGEXP %s 
                      OR macyorder.`Offer SKU` REGEXP %s 
                      OR macyorder.Costway_SKU REGEXP %s 
                      OR CostwayOrder REGEXP %s

                   UNION ALL

                   SELECT 
                       'Bestbuy' AS Source, 
                       CASE
                           WHEN t1.SKU IS NOT NULL THEN '璞泤'
                           WHEN t2.SKU IS NOT NULL THEN '鍙搁『'
                           WHEN t3.SKU IS NOT NULL THEN '澶у缓'
                           WHEN t4.SKU IS NOT NULL THEN '鑷存'
                           ELSE ''
                       END AS SupplierSource,
                       `Order number` AS OrderID, 
                       CostwayOrder,
                       Costway_SKU AS CostwaySKU,
                       CONCAT(`Shipping address first name`, ' ', `Shipping address last name`) AS FullName,
                       bestbuyorder.`Offer SKU` AS SKU, 
                       Quantity AS Qty, 
                       `Date created` AS Date, 
                       Status,
                       Tracking
                   FROM bestbuyorder
                   LEFT JOIN newestdropship t1 
                      ON t1.SKU COLLATE utf8mb4_unicode_ci = bestbuyorder.Costway_SKU COLLATE utf8mb4_unicode_ci
                   LEFT JOIN newestdropship_vevor t2 
                      ON t2.SKU COLLATE utf8mb4_unicode_ci = bestbuyorder.Costway_SKU COLLATE utf8mb4_unicode_ci
                   LEFT JOIN newestdropship_dajian t3 
                      ON t3.SKU COLLATE utf8mb4_unicode_ci = bestbuyorder.Costway_SKU COLLATE utf8mb4_unicode_ci
                   LEFT JOIN newestdropship_songmics t4 
                      ON t4.SKU COLLATE utf8mb4_unicode_ci = bestbuyorder.Costway_SKU COLLATE utf8mb4_unicode_ci
                   WHERE `Shipping address first name` REGEXP %s 
                      OR `Shipping address last name` REGEXP %s 
                      OR `Order number` REGEXP %s 
                      OR bestbuyorder.`Offer SKU` REGEXP %s 
                      OR bestbuyorder.Costway_SKU REGEXP %s 
                      OR CostwayOrder REGEXP %s
                """
                params = [regex_pattern] * 18
                cursor.execute(query, params)
                return cursor.fetchall()
        finally:
            conn.close()

    def get_last_stock_update_time():
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cursor:
                sql = "SELECT MAX(Updated_At) as last_time FROM newestdropship"
                cursor.execute(sql)
                result = cursor.fetchone()
                if result and result.get("last_time"):
                    return str(result["last_time"])
                return "暂无记录"
        except Exception as e:
            return f"查询失败: {e}"
        finally:
            conn.close()

    # =======================
    # ✅ Costway Upsert（保持原逻辑）
    # =======================
    @staticmethod
    def update_costway_stock(data_tuples):
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cursor:
                sql = """
                    INSERT INTO newestdropship (SKU, Price, Stock, Updated_At)
                    VALUES (%s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        Price = VALUES(Price),
                        Stock = VALUES(Stock),
                        Updated_At = VALUES(Updated_At);
                """
                cursor.executemany(sql, data_tuples)
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    # =======================
    # ✅ ✅ ✅ Vevor（重点修复：不清空，改为 Upsert，更快）
    # =======================
    @staticmethod
    def rewrite_vevor_stock(data_tuples, batch_size=2000):
        """
        ✅ Updated_At 使用固定时间（和 costway 一样）
        ✅ 分批写入 + 进度打印（避免卡死）
        """
        conn = DBManager.get_connection()
        total = len(data_tuples)
        if total == 0:
            return

        now = datetime.now()  # ✅ 固定时间

        # data_tuples: [(sku, price, stock), ...] -> 加上 now
        data_with_time = [(sku, price, stock, now) for (sku, price, stock) in data_tuples]

        try:
            with conn.cursor() as cursor:
                sql = """
                    INSERT INTO newestdropship_vevor (SKU, Price, Stock, Updated_At)
                    VALUES (%s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        Price = VALUES(Price),
                        Stock = VALUES(Stock),
                        Updated_At = VALUES(Updated_At);
                """

                for i in range(0, total, batch_size):
                    cursor.executemany(sql, data_with_time[i:i + batch_size])
                    conn.commit()

        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    # =======================
    # ✅ GIGA
    # =======================
    @staticmethod
    def get_all_giga_skus():
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT SKU FROM newestdropship_dajian")
                results = cursor.fetchall()

                skus = []
                for row in results:
                    if not row:
                        continue

                    # 如果 row 是 dict
                    if isinstance(row, dict):
                        sku = row.get("SKU")

                    # 如果 row 是 tuple / list
                    else:
                        sku = row[0] if len(row) > 0 else None

                    if sku:
                        skus.append(str(sku).strip())

                return skus
        finally:
            conn.close()

    @staticmethod
    def update_giga_stock(data_tuples, batch_size=5000):
        conn = DBManager.get_connection()
        total = len(data_tuples)
        if total == 0:
            return

        try:
            with conn.cursor() as cursor:
                sql = """
                    INSERT INTO newestdropship_dajian (SKU, Price, Stock, Updated_At)
                    VALUES (%s, %s, %s, NOW())
                    ON DUPLICATE KEY UPDATE
                        Price = COALESCE(VALUES(Price), Price),
                        Stock = COALESCE(VALUES(Stock), Stock),
                        Updated_At = NOW();
                """

                # data_tuples 原来是 (price, stock, sku)
                # 这里要变成 (sku, price, stock)
                data_tuples = [(sku, price, stock) for (price, stock, sku) in data_tuples]

                for i in range(0, total, batch_size):
                    cursor.executemany(sql, data_tuples[i:i + batch_size])
                    conn.commit()

        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    @staticmethod
    def insert_bestbuy_orders(data_tuples):
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cursor:
                sql = """
                INSERT INTO bestbuyorder (
                    `Order number`, `Order line no.`, `Date created`, `Shipping address first name`,
                    `Shipping address last name`, `Shipping address street 1`, `Shipping address street 2`,
                    `Shipping address country`, `Shipping address city`, `Shipping address state`,
                    `Shipping address zip`, `Quantity`, `Offer SKU`, `Unit price`, `CostwayOrder`, `Status`
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, '未发货')
                ON DUPLICATE KEY UPDATE
                    `Order line no.` = VALUES(`Order line no.`),
                    `Date created` = VALUES(`Date created`),
                    `Status` = `Status`
                """
                cursor.executemany(sql, data_tuples)
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    @staticmethod
    def insert_walmart_orders(data_tuples):
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cursor:
                sql = """
                INSERT INTO walmartorder (
                    PO_Number, Order_Number, Order_Date, First_Name, Last_Name,
                    Ship_to_Address1, Ship_to_Address2, Ship_to_Country, City, State,
                    Zip, Qty, SKU, Item_Cost, CostwayOrder, Status
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, '未发货')
                ON DUPLICATE KEY UPDATE
                    Order_Date = VALUES(Order_Date),
                    Qty = VALUES(Qty),
                    Status = `Status`
                """
                cursor.executemany(sql, data_tuples)
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    @staticmethod
    def update_songmics_stock(data_tuples):
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cursor:
                sql = '''
                    INSERT INTO newestdropship_songmics (SKU, Price, Stock, Updated_At)
                    VALUES (%s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        Price = VALUES(Price),
                        Stock = VALUES(Stock),
                        Updated_At = VALUES(Updated_At);
                '''
                cursor.executemany(sql, data_tuples)
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    @staticmethod
    def get_supplier_max_stock(sku: str) -> int:
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cursor:
                sql = '''
                SELECT MAX(stock_val) AS max_stock
                FROM (
                    SELECT Stock AS stock_val FROM newestdropship WHERE SKU = %s
                    UNION ALL
                    SELECT Stock AS stock_val FROM newestdropship_vevor WHERE SKU = %s
                    UNION ALL
                    SELECT Stock AS stock_val FROM newestdropship_dajian WHERE SKU = %s
                    UNION ALL
                    SELECT Stock AS stock_val FROM newestdropship_songmics WHERE SKU = %s
                ) t
                '''
                cursor.execute(sql, (sku, sku, sku, sku))
                row = cursor.fetchone()
                if not row:
                    return 0
                val = row.get("max_stock") if isinstance(row, dict) else row[0]
                return int(val) if val is not None else 0
        finally:
            conn.close()

    @staticmethod
    def get_supplier_max_stock_with_source(sku: str):
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cursor:
                sql = """
                SELECT stock_val, source
                FROM (
                    SELECT Stock AS stock_val, 'haoya' AS source FROM newestdropship WHERE SKU = %s
                    UNION ALL
                    SELECT Stock AS stock_val, 'sishun' AS source FROM newestdropship_vevor WHERE SKU = %s
                    UNION ALL
                    SELECT Stock AS stock_val, 'dajian' AS source FROM newestdropship_dajian WHERE SKU = %s
                    UNION ALL
                    SELECT Stock AS stock_val, 'songmics' AS source FROM newestdropship_songmics WHERE SKU = %s
                ) t
                ORDER BY stock_val DESC
                LIMIT 1
                """
                cursor.execute(sql, (sku, sku, sku, sku))
                row = cursor.fetchone()
                if not row:
                    return 0, None
                stock_val = row.get("stock_val") if isinstance(row, dict) else row[0]
                source = row.get("source") if isinstance(row, dict) else row[1]
                try:
                    stock_val = int(stock_val)
                except Exception:
                    stock_val = 0
                return stock_val, source
        finally:
            conn.close()

    @staticmethod
    def get_shop_stock_max_map():
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT SKU, warehouse_SKU FROM mapping_table")
                mapping_rows = cursor.fetchall()

            if not mapping_rows:
                return {}

            warehouse_skus = [r["warehouse_SKU"] for r in mapping_rows if r.get("warehouse_SKU")]
            warehouse_skus = list({str(s).strip() for s in warehouse_skus if str(s).strip()})
            if not warehouse_skus:
                return {}

            def _fetch_table_stock(table, skus):
                if not skus:
                    return {}
                out = {}
                chunk_size = 1000
                with conn.cursor() as cursor:
                    for i in range(0, len(skus), chunk_size):
                        chunk = skus[i:i + chunk_size]
                        placeholders = ",".join(["%s"] * len(chunk))
                        sql = f"SELECT SKU, Stock FROM {table} WHERE SKU IN ({placeholders})"
                        cursor.execute(sql, chunk)
                        for row in cursor.fetchall():
                            sku_val = str(row["SKU"]).strip()
                            out[sku_val] = row.get("Stock")
                return out

            table_sources = [
                ("haoya", "newestdropship"),
                ("sishun", "newestdropship_vevor"),
                ("dajian", "newestdropship_dajian"),
                ("songmics", "newestdropship_songmics"),
            ]

            supplier_map = {}
            for source_key, table in table_sources:
                table_data = _fetch_table_stock(table, warehouse_skus)
                for sku_val, stock_val in table_data.items():
                    try:
                        stock_num = int(stock_val) if stock_val is not None and stock_val != "" else 0
                    except Exception:
                        stock_num = 0
                    current = supplier_map.get(sku_val)
                    if current is None or stock_num > current[0]:
                        supplier_map[sku_val] = (stock_num, source_key)

            shop_map = {}
            for r in mapping_rows:
                shop_sku = str(r.get("SKU") or "").strip()
                wh_sku = str(r.get("warehouse_SKU") or "").strip()
                if not shop_sku or not wh_sku:
                    continue
                stock_info = supplier_map.get(wh_sku, (0, None))
                shop_map[shop_sku] = stock_info

            return shop_map
        finally:
            conn.close()

    # =======================
    # ✅ Bestbuy（你原逻辑保留）
    # =======================
    @staticmethod
    def get_bestbuy_stock_map():
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cursor:
                query = """
                SELECT 
                    m.SKU AS ShopSKU,
                    COALESCE(t1.Stock, t2.Stock, t3.Stock, t4.Stock, 0) AS FinalStock
                FROM mapping_table m
                LEFT JOIN newestdropship t1 
                    ON m.warehouse_SKU COLLATE utf8mb4_unicode_ci = t1.SKU COLLATE utf8mb4_unicode_ci
                LEFT JOIN newestdropship_vevor t2 
                    ON m.warehouse_SKU COLLATE utf8mb4_unicode_ci = t2.SKU COLLATE utf8mb4_unicode_ci
                LEFT JOIN newestdropship_dajian t3 
                    ON m.warehouse_SKU COLLATE utf8mb4_unicode_ci = t3.SKU COLLATE utf8mb4_unicode_ci
                LEFT JOIN newestdropship_songmics t4 
                    ON m.warehouse_SKU COLLATE utf8mb4_unicode_ci = t4.SKU COLLATE utf8mb4_unicode_ci
                """
                cursor.execute(query)
                results = cursor.fetchall()
                return {str(row["ShopSKU"]).strip(): int(row["FinalStock"]) for row in results}
        finally:
            conn.close()

    # =======================
    # 用户登录
    # =======================
    @staticmethod
    def get_user_by_username(username):
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cursor:
                sql = """
                SELECT id, username, password_hash, is_active
                FROM users
                WHERE username = %s
                LIMIT 1
                """
                cursor.execute(sql, (username,))
                return cursor.fetchone()
        finally:
            conn.close()

    @staticmethod
    def update_user_last_login(user_id):
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE users SET last_login_at = NOW() WHERE id = %s",
                    (user_id,),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


    @staticmethod
    def fetch_unshipped_orders():
        conn = DBManager.get_connection()
        cursor = conn.cursor()  # 推荐 dictionary=True，这样 fetchall 返回 dict
        # **查询 walmartorder 未发货订单**
        walmart_query = """
        SELECT 
            'sskyn36@outlook.com' AS customer_email,
            Costway_SKU AS sku,
            Qty AS qty,
            CASE 
                WHEN Ship_to_Address2 IS NULL OR Ship_to_Address2 = '' THEN Ship_to_Address1 
                ELSE CONCAT(Ship_to_Address1, ', ', Ship_to_Address2) 
            END AS street,
            `City` AS city,
            `State` AS region,
            `Zip` AS postcode,
            `First_Name` AS first_name,
            `Last_Name` AS last_name,
            `Order_Number` AS order_number,
            `CostwayOrder` AS costway_number,
            '' AS line_item_number,
            `Order_Date` AS order_date,
            `SKU` AS platform_sku

        FROM walmartorder
        WHERE Status = '未发货'
        """

        cursor.execute(walmart_query)
        walmart_data = cursor.fetchall()

        # **查询 macyorder 未发货订单**
        macy_query = """
        SELECT 
            'sskyn36@outlook.com' AS customer_email,
            Costway_SKU AS sku,
            Quantity AS qty,
            CASE 
                WHEN `Shipping address street 2` IS NULL OR `Shipping address street 2` = '' 
                THEN `Shipping address street 1`
                ELSE CONCAT(`Shipping address street 1`, ', ', `Shipping address street 2`) 
            END AS street,
            `Shipping address city` AS city,
            `Shipping address state` AS region,
            `Shipping address zip` AS postcode,
            `Shipping address first name` AS first_name,
            `Shipping address last name` AS last_name,
            `Order number` AS order_number,
            `CostwayOrder` AS costway_number,
            `Order line no.` AS line_item_number,
            `Date created` AS order_date,
            `Offer SKU` AS platform_sku
        FROM macyorder
        WHERE Status = '未发货'
        """

        cursor.execute(macy_query)
        macy_data = cursor.fetchall()

        # **查询 bestbuyorder 未发货订单**
        bestbuy_query = """
        SELECT 
            'sskyn36@outlook.com' AS customer_email,
            Costway_SKU AS sku,
            Quantity AS qty,
            CASE 
                WHEN `Shipping address street 2` IS NULL OR `Shipping address street 2` = '' 
                THEN `Shipping address street 1`
                ELSE CONCAT(`Shipping address street 1`, ', ', `Shipping address street 2`) 
            END AS street,
            `Shipping address city` AS city,
            `Shipping address state` AS region,
            `Shipping address zip` AS postcode,
            `Shipping address first name` AS first_name,
            `Shipping address last name` AS last_name,
            `Order number` AS order_number,
            `CostwayOrder` AS costway_number,
            `Order line no.` AS line_item_number,
            `Date created` AS order_date,
            `Offer SKU` AS platform_sku
        FROM bestbuyorder
        WHERE Status = '未发货'
        """

        cursor.execute(bestbuy_query)
        bestbuy_data = cursor.fetchall()

        conn.close()

        # 合并两个查询结果
        return walmart_data + macy_data + bestbuy_data

    @staticmethod
    def fetch_unshipped_orders_for_manual():
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cursor:
                shipped_status = "已发货"
                unshipped_status = "未发货"
                unshipped_status_alt = "未發貨"

                walmart_query = """
                SELECT
                    'walmart' AS platform,
                    'Walmart' AS platform_label,
                    PO_Number AS po_number,
                    Order_Number AS order_number,
                    '' AS order_line_no,
                    CostwayOrder AS costway_order,
                    SKU AS sku,
                    Qty AS qty,
                    CONCAT(First_Name, ' ', Last_Name) AS customer_name,
                    Order_Date AS order_date,
                    Tracking,
                    Status
                FROM walmartorder
                WHERE Status IS NULL OR Status = '' OR Status = %s OR Status = %s
                """
                cursor.execute(walmart_query, (unshipped_status, unshipped_status_alt))
                walmart_data = cursor.fetchall()

                macy_query = """
                SELECT
                    'macy' AS platform,
                    'Macy' AS platform_label,
                    '' AS po_number,
                    `Order number` AS order_number,
                    `Order line no.` AS order_line_no,
                    CostwayOrder AS costway_order,
                    `Offer SKU` AS sku,
                    Quantity AS qty,
                    CONCAT(`Shipping address first name`, ' ', `Shipping address last name`) AS customer_name,
                    `Date created` AS order_date,
                    Tracking,
                    Status
                FROM macyorder
                WHERE Status IS NULL OR Status = '' OR Status = %s OR Status = %s
                """
                cursor.execute(macy_query, (unshipped_status, unshipped_status_alt))
                macy_data = cursor.fetchall()

                bestbuy_query = """
                SELECT
                    'bestbuy' AS platform,
                    'Bestbuy' AS platform_label,
                    '' AS po_number,
                    `Order number` AS order_number,
                    `Order line no.` AS order_line_no,
                    CostwayOrder AS costway_order,
                    `Offer SKU` AS sku,
                    Quantity AS qty,
                    CONCAT(`Shipping address first name`, ' ', `Shipping address last name`) AS customer_name,
                    `Date created` AS order_date,
                    Tracking,
                    Status
                FROM bestbuyorder
                WHERE Status IS NULL OR Status = '' OR Status = %s OR Status = %s
                """
                cursor.execute(bestbuy_query, (unshipped_status, unshipped_status_alt))
                bestbuy_data = cursor.fetchall()

                return walmart_data + macy_data + bestbuy_data
        finally:
            conn.close()

    @staticmethod
    def bulk_update_tracking_by_costwayorder(rows):
        if not rows:
            return []

        table_map = {
            "macy": "macyorder",
            "walmart": "walmartorder",
            "bestbuy": "bestbuyorder",
        }

        conn = DBManager.get_connection()
        results = []
        try:
            with conn.cursor() as cursor:
                for row in rows:
                    platform = (row.get("platform") or "").strip().lower()
                    costway_order = (row.get("costway_order") or "").strip()
                    tracking = (row.get("tracking") or "").strip()

                    table = table_map.get(platform)
                    if not table:
                        results.append({
                            "platform": platform,
                            "costway_order": costway_order,
                            "tracking": tracking,
                            "ok": False,
                            "msg": "unsupported_platform",
                        })
                        continue

                    if not costway_order:
                        results.append({
                            "platform": platform,
                            "costway_order": costway_order,
                            "tracking": tracking,
                            "ok": False,
                            "msg": "missing_costway_order",
                        })
                        continue

                    if not tracking or tracking == "0":
                        results.append({
                            "platform": platform,
                            "costway_order": costway_order,
                            "tracking": tracking,
                            "ok": False,
                            "msg": "missing_tracking",
                        })
                        continue

                    sql = f"UPDATE {table} SET Tracking = %s, Status = %s WHERE CostwayOrder = %s"
                    cursor.execute(sql, (tracking, "已发货", costway_order))
                    if cursor.rowcount > 0:
                        results.append({
                            "platform": platform,
                            "costway_order": costway_order,
                            "tracking": tracking,
                            "ok": True,
                            "msg": "updated",
                        })
                    else:
                        results.append({
                            "platform": platform,
                            "costway_order": costway_order,
                            "tracking": tracking,
                            "ok": False,
                            "msg": "not_found",
                        })

            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        return results

    @staticmethod
    def fetch_sku_map(table_name):
        conn = DBManager.get_connection()
        cursor = conn.cursor()
        try:
            # 根据截图，字段名为 SKU 和 Price
            sql = f"SELECT SKU, Price FROM {table_name}"
            cursor.execute(sql)
            results = cursor.fetchall()
            # 生成字典: {'SKU123': 100.00, 'SKU456': 50.00}
            return {row['SKU']: row['Price'] for row in results}
        finally:
            cursor.close()
            conn.close()

    @staticmethod
    def update_costwaymacy_sku():
        conn = DBManager.get_connection()
        cursor = conn.cursor()

        update_query = """
            UPDATE macyorder wo
            JOIN mapping_table m ON wo.`Offer SKU` = m.SKU
            SET wo.Costway_SKU = m.warehouse_SKU
            WHERE wo.Costway_SKU IS NULL OR wo.Costway_SKU = '';
            """

        cursor.execute(update_query)
        conn.commit()
        cursor.close()
        conn.close()

        print("Costway_SKU (macyorder) updated")

    @staticmethod
    def update_costwaybestbuy_sku():
        conn = DBManager.get_connection()
        cursor = conn.cursor()

        update_query = """
            UPDATE bestbuyorder bo
            JOIN mapping_table m ON bo.`Offer SKU` = m.SKU
            SET bo.Costway_SKU = m.warehouse_SKU
            WHERE bo.Costway_SKU IS NULL OR bo.Costway_SKU = ''
            """

        cursor.execute(update_query)
        conn.commit()
        cursor.close()
        conn.close()

        print("Costway_SKU (bestbuyorder) updated")

    @staticmethod
    def update_costwaywalmart_sku():
        conn = DBManager.get_connection()
        cursor = conn.cursor()

        update_query = """
            UPDATE walmartorder wo
            JOIN mapping_table m ON wo.SKU = m.SKU
            SET wo.Costway_SKU = m.warehouse_SKU
            WHERE wo.Costway_SKU IS NULL OR wo.Costway_SKU = ''
            """

        cursor.execute(update_query)
        conn.commit()
        cursor.close()
        conn.close()

        print("Costway_SKU (walmartorder) updated")

    @staticmethod
    def upsert_mapping_table(data_tuples):
        if not data_tuples:
            return 0
        conn = DBManager.get_connection()
        try:
            with conn.cursor() as cursor:
                sql = """
                    INSERT INTO mapping_table (SKU, warehouse_SKU)
                    VALUES (%s, %s)
                    ON DUPLICATE KEY UPDATE
                        warehouse_SKU = VALUES(warehouse_SKU);
                """
                cursor.executemany(sql, data_tuples)
            conn.commit()
            return len(data_tuples)
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

