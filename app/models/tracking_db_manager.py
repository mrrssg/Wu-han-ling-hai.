import pymysql
from flask import current_app
from datetime import datetime
import time


class Tracking_DBManager:
    @staticmethod
    def get_connection():
        """
        ✅ 必须用 DictCursor：
        - 你很多地方 row['SKU'] / row['last_time'] / row['CostwayOrder'] 都依赖 dict
        - 用普通 Cursor 会返回 tuple，导致 tuple indices 报错
        """
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
            autocommit=False
        )

    # =======================
    # ✅ Macy / Walmart 序号

    @staticmethod
    def research_macyorder():
        try:
            conn = Tracking_DBManager.get_connection()
            cursor = conn.cursor()

            query = """
                SELECT 
                    `Order number`, 
                    `Order line no.`, 
                    `Costway_SKU`, 
                    `Shipping address first name`, 
                    `Shipping address last name`, 
                    `Shipping address street 1`, 
                    `Shipping address street 2`, 
                    `Shipping address city`, 
                    `Shipping address state`,
                    `CostwayOrder`
                FROM `macyorder`
                WHERE `Status` = '未发货'
            """

            cursor.execute(query)
            data = cursor.fetchall()  # 获取数据

            print("Macy未发货数据查询完成！")
            return data  # 返回数据

        except Exception as e:
            print(f"数据库查询失败: {e}")
            return None  # 出错时返回 None

        finally:
            # 确保连接在任何情况下都被关闭
            conn.close()

    @staticmethod
    def research_walmartorder():
        try:
            conn = Tracking_DBManager.get_connection()
            cursor = conn.cursor()

            query = """
            SELECT PO_Number, Costway_SKU, First_Name, Last_Name, Ship_to_Address1, Ship_to_Address2, City, State, CostwayOrder
            FROM walmartorder
            WHERE Status = '未发货'
            """
            cursor.execute(query)
            data = cursor.fetchall()  # 获取数据

            print("Walmart未发货数据查询完成！")
            return data  # 返回数据

        except Exception as e:
            print(f"数据库查询失败: {e}")
            return None  # 出错时返回 None

        finally:
            # 确保连接在任何情况下都被关闭
            conn.close()

    @staticmethod
    def research_bestbuyorder():
        try:
            conn = Tracking_DBManager.get_connection()
            cursor = conn.cursor()

            query = """
                SELECT 
                    `Order number`, 
                    `Order line no.`, 
                    `Costway_SKU`, 
                    `Shipping address first name`, 
                    `Shipping address last name`, 
                    `Shipping address street 1`, 
                    `Shipping address street 2`, 
                    `Shipping address city`, 
                    `Shipping address state`,
                    `CostwayOrder`
                FROM `bestbuyorder`
                WHERE `Status` = '未发货'
            """

            cursor.execute(query)
            data = cursor.fetchall()
            print("Bestbuy未发货数据查询完成！")
            return data

        except Exception as e:
            print(f"数据库查询失败: {e}")
            return None

        finally:
            conn.close()

    @staticmethod
    def update_macyorder(match_data):
        import pandas as pd

        conn = None
        cursor = None
        try:
            conn = Tracking_DBManager.get_connection()
            cursor = conn.cursor()

            if isinstance(match_data, list):
                df = pd.DataFrame(match_data)
            else:
                df = match_data.copy()

            # 统一列名
            df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]

            for _, row in df.iterrows():
                order_number = str(row["order_number"]).strip()

                tracking = row.get("tracking", "")
                tracking = "" if pd.isna(tracking) else str(tracking).strip()

                # ✅ 只更新 CostwayDiscount
                special_offer = None
                if "special_offer" in df.columns and pd.notna(row.get("special_offer")):
                    special_offer = float(row["special_offer"])

                status = "已发货" if tracking else None

                if status:
                    query = """
                        UPDATE macyorder
                        SET Tracking=%s, CostwayDiscount=%s, Status=%s
                        WHERE `Order number`=%s
                    """
                    values = (tracking, special_offer, status, order_number)
                else:
                    query = """
                        UPDATE macyorder
                        SET Tracking=%s, CostwayDiscount=%s
                        WHERE `Order number`=%s
                    """
                    values = (tracking, special_offer, order_number)

                cursor.execute(query, values)

            conn.commit()
            print("✅ 更新完成（未更新 CostwayOrder）")

        except Exception as e:
            if conn:
                conn.rollback()
            print(f"❌ 更新数据库失败: {e}")

        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

    @staticmethod
    def update_walmartorder(match_data):
        import pandas as pd
        conn = None
        cursor = None
        try:
            conn = Tracking_DBManager.get_connection()
            cursor = conn.cursor()

            if isinstance(match_data, list):
                df = pd.DataFrame(match_data)
            else:
                df = match_data.copy()

            df = df.rename(columns=lambda x: str(x).strip().lower().replace(" ", "_"))
            print(df)

            for _, row in df.iterrows():
                order_number = str(row["order_number"]).strip()

                tracking = row.get("tracking", "")
                if tracking is None or (isinstance(tracking, float) and pd.isna(tracking)):
                    tracking = row.get("unnamed:_2", "")
                tracking = "" if pd.isna(tracking) else str(tracking).strip()

                special_offer = None
                if pd.notna(row.get("special_offer", None)):
                    special_offer = float(row["special_offer"])

                status = "已发货" if tracking else None

                if status:
                    query = """
                        UPDATE walmartorder
                        SET Tracking=%s, CostwayDiscount=%s, Status=%s
                        WHERE `PO_Number`=%s
                    """
                    values = (tracking, special_offer, status, order_number)
                else:
                    query = """
                        UPDATE walmartorder
                        SET Tracking=%s, CostwayDiscount=%s
                        WHERE `PO_Number`=%s
                    """
                    values = (tracking, special_offer, order_number)

                cursor.execute(query, values)

            conn.commit()
            print("✅ 订单状态更新完成（未更新 CostwayOrder）")

        except Exception as e:
            if conn:
                conn.rollback()
            print(f"❌ 更新数据库失败: {e}")

        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

    @staticmethod
    def update_bestbuyorder(match_data):
        import pandas as pd
        conn = None
        cursor = None
        try:
            conn = Tracking_DBManager.get_connection()
            cursor = conn.cursor()

            if isinstance(match_data, list):
                df = pd.DataFrame(match_data)
            else:
                df = match_data.copy()

            df = df.rename(columns=lambda x: str(x).strip().lower().replace(" ", "_"))

            for _, row in df.iterrows():
                order_number = str(row["order_number"]).strip()

                tracking = row.get("tracking", "")
                tracking = "" if pd.isna(tracking) else str(tracking).strip()

                special_offer = None
                if pd.notna(row.get("special_offer", None)):
                    special_offer = float(row["special_offer"])

                status = "已发货" if tracking else None

                if status:
                    query = """
                        UPDATE bestbuyorder
                        SET Tracking=%s, CostwayDiscount=%s, Status=%s
                        WHERE `Order number`=%s
                    """
                    values = (tracking, special_offer, status, order_number)
                else:
                    query = """
                        UPDATE bestbuyorder
                        SET Tracking=%s, CostwayDiscount=%s
                        WHERE `Order number`=%s
                    """
                    values = (tracking, special_offer, order_number)

                cursor.execute(query, values)

            conn.commit()
            print("Bestbuy订单状态更新完成（未更新CostwayOrder）")

        except Exception as e:
            if conn:
                conn.rollback()
            print(f"更新数据库失败: {e}")

        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()
    # @staticmethod
    # def estimated_profit(match):
    #     try:
    #         conn = Tracking_DBManager.get_connection()
    #         cursor = conn.cursor()
    #
    #         # ✅ 获取匹配的订单号列表（兼容 list / DataFrame）
    #         if isinstance(match, list):
    #             order_numbers = tuple(match)
    #         else:
    #             order_numbers = tuple(match["order_number"].unique())
    #
    #         if not order_numbers:  # 如果没有匹配的订单号，直接返回
    #             return
    #
    #         query = """
    #             UPDATE macyorder
    #             SET
    #                 EstimatedProfit = (CAST(`Unit price` AS DECIMAL(10,2)) * Quantity * 0.82 - CostwayDiscount / (1 - 0.75) * 0.75),
    #                 `Estimated rate of profit` = CASE
    #                     WHEN `Unit price` > 0 THEN EstimatedProfit / (`Unit price` * Quantity)
    #                     ELSE NULL
    #                 END
    #             WHERE `Order number` IN ({})
    #         """.format(','.join(['%s'] * len(order_numbers)))
    #
    #         cursor.execute(query, order_numbers)
    #         conn.commit()  # 提交事务
    #
    #     except Exception as e:
    #         print(f"Error updating EstimatedProfit: {e}")
    #         return None  # 出错时返回 None
    #
    #     finally:
    #         conn.close()  # 确保数据库连接被关闭

    @staticmethod
    def estimated_profit(match):
        try:
            conn = Tracking_DBManager.get_connection()
            cursor = conn.cursor()

            # ✅ 兼容 list / DataFrame
            if isinstance(match, list):
                order_numbers = tuple(match)
                df = None
            else:
                df = match.copy()
                df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
                order_numbers = tuple(df["order_number"].unique())

            if not order_numbers:
                return

            # =====================================================
            # ✅ Step1：如果 df 里有 sku，则先更新 CostwayDiscount
            # =====================================================
            if df is not None and "sku" in df.columns:
                sku_list = df["sku"].dropna().astype(str).str.strip().unique().tolist()

                # ✅ SKU -> Discount 映射
                discount_map = {}

                for sku in sku_list:
                    price = None

                    # ✅ newestdropship 优先查
                    cursor.execute("SELECT Price FROM newestdropship WHERE SKU=%s LIMIT 1", (sku,))
                    row = cursor.fetchone()
                    if row:
                        price = float(row["Price"])
                        discount_map[sku] = round(price * 0.75, 4)
                        continue


                    # ✅ vevor 表
                    cursor.execute("SELECT Price FROM newestdropship_vevor WHERE SKU=%s LIMIT 1", (sku,))
                    row = cursor.fetchone()
                    if row:
                        price = float(row["Price"])
                        # ⚠️ 这里暂时先用 0.25，你后续告诉我规则再改
                        discount_map[sku] = round(price * 0.9, 4)
                        continue

                # ✅ 批量更新 macyorder 的 CostwayDiscount
                for _, r in df.iterrows():
                    sku = str(r.get("sku", "")).strip()
                    order_number = str(r.get("order_number", "")).strip()

                    if not sku or not order_number:
                        continue

                    if sku in discount_map:
                        cursor.execute("""
                            UPDATE macyorder
                            SET CostwayDiscount=%s
                            WHERE `Order number`=%s
                        """, (discount_map[sku], order_number))

                conn.commit()

            # =====================================================
            # ✅ Step2：再执行利润核算 SQL（你原来的逻辑）
            # =====================================================
            query = """
                UPDATE macyorder
                SET 
                    EstimatedProfit = (CAST(`Unit price` AS DECIMAL(10,2)) * Quantity * 0.82 - CostwayDiscount),
                    `Estimated rate of profit` = CASE 
                        WHEN `Unit price` > 0 THEN EstimatedProfit / (`Unit price` * Quantity)
                        ELSE NULL 
                    END
                WHERE `Order number` IN ({})
            """.format(','.join(['%s'] * len(order_numbers)))

            cursor.execute(query, order_numbers)
            conn.commit()

        except Exception as e:
            print(f"Error updating EstimatedProfit: {e}")
            return None

        finally:
            conn.close()


    # @staticmethod
    # def walmart_estimated_profit(match):
    #     try:
    #         conn = Tracking_DBManager.get_connection()
    #         cursor = conn.cursor()
    #
    #         # ✅ 获取匹配的订单号列表（兼容 list / DataFrame）
    #         if isinstance(match, list):
    #             order_numbers = tuple(match)
    #         else:
    #             order_numbers = tuple(match["order_number"].unique())
    #
    #         if not order_numbers:  # 如果没有匹配的订单号，直接返回
    #             return
    #
    #         query = """
    #             UPDATE walmartorder
    #             SET
    #                 EstimatedProfit = (CAST(`Item_Cost` AS DECIMAL(10,2)) * Qty * 0.85 - CostwayDiscount / (1 - 0.75) * 0.75),
    #                 `Estimated rate of profit` = CASE
    #                     WHEN `Item_Cost` > 0 THEN EstimatedProfit / (`Item_Cost`*Qty)
    #                     ELSE NULL
    #                 END
    #             WHERE `PO_Number` IN ({})
    #         """.format(','.join(['%s'] * len(order_numbers)))
    #
    #         cursor.execute(query, order_numbers)
    #         conn.commit()  # 提交事务
    #
    #     except Exception as e:
    #         print(f"Error updating EstimatedProfit: {e}")
    #         return None  # 出错时返回 None
    #
    #     finally:
    #         conn.close()  # 确保数据库连接被关闭


    @staticmethod
    def walmart_estimated_profit(match):
        return _walmart_estimated_profit(match)

@staticmethod
def _walmart_estimated_profit(match):
    try:
        conn = Tracking_DBManager.get_connection()
        cursor = conn.cursor()

        # ✅ 兼容 list / DataFrame
        if isinstance(match, list):
            order_numbers = tuple(match)
            df = None
        else:
            df = match.copy()
            df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
            order_numbers = tuple(df["order_number"].unique())

        if not order_numbers:
            return

        # =====================================================
        # ✅ Step1：如果 df 里有 sku，则先更新 CostwayDiscount
        # =====================================================
        if df is not None and "sku" in df.columns:
            sku_list = df["sku"].dropna().astype(str).str.strip().unique().tolist()

            discount_map = {}

            for sku in sku_list:
                # ✅ newestdropship
                cursor.execute("SELECT Price FROM newestdropship WHERE SKU=%s LIMIT 1", (sku,))
                row = cursor.fetchone()
                if row:
                    price = float(row["Price"])
                    discount_map[sku] = round(price * 0.75, 4)
                    continue


            # ✅ 批量更新 walmartorder.CostwayDiscount
            for _, r in df.iterrows():
                sku = str(r.get("sku", "")).strip()
                order_number = str(r.get("order_number", "")).strip()  # 这里就是 PO_Number

                if not sku or not order_number:
                    continue

                if sku in discount_map:
                    cursor.execute("""
                            UPDATE walmartorder
                            SET CostwayDiscount=%s
                            WHERE `PO_Number`=%s
                        """, (discount_map[sku], order_number))

            conn.commit()

        # =====================================================
        # ✅ Step2：利润核算 SQL（你原来的逻辑）
        # =====================================================
        query = """
                UPDATE walmartorder
                SET 
                    EstimatedProfit = (CAST(`Item_Cost` AS DECIMAL(10,2)) * Qty * 0.85 - CostwayDiscount),
                    `Estimated rate of profit` = CASE 
                        WHEN `Item_Cost` > 0 THEN EstimatedProfit / (`Item_Cost`*Qty)
                        ELSE NULL 
                    END
                WHERE `PO_Number` IN ({})
            """.format(','.join(['%s'] * len(order_numbers)))

        cursor.execute(query, order_numbers)
        conn.commit()

    except Exception as e:
        print(f"Error updating EstimatedProfit: {e}")
        return None

    finally:
        conn.close()
