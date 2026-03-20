import pandas as pd
from datetime import datetime
from app.models.db_manager import DBManager


class OrderService:
    @staticmethod
    def process_macy_orders(file_path):
        try:
            # 1. 读取 Excel
            try:
                df = pd.read_excel(file_path, sheet_name="orders", dtype={"Shipping address zip": str})
            except:
                df = pd.read_excel(file_path, sheet_name=0, dtype={"Shipping address zip": str})

            # 2. 格式化日期
            df["Date created"] = pd.to_datetime(df["Date created"], errors='coerce').dt.strftime('%Y-%m-%d')

            # 3. 按【Order number + Order line no.】统一分配 Order number
            df["Order number"] = (
                df["Order number"].where(df["Order number"].notna(), "").astype(str).str.strip()
            )
            df["Order line no."] = (
                df["Order line no."].where(df["Order line no."].notna(), "").astype(str).str.strip()
            )
            order_line_pairs = list(zip(df["Order number"].tolist(), df["Order line no."].tolist()))
            df["Order number"] = DBManager.assign_macy_order_numbers(order_line_pairs)

            # 4. 生成 CostwayOrder
            start_seq = DBManager.get_macy_max_sequence()
            date_str = datetime.now().strftime("%y%m%d")

            # 填充 CostwayOrder 列
            df['CostwayOrder'] = [f"WHLH{date_str}-{start_seq + i + 1}" for i in range(len(df))]

            # 5. 准备入库数据 (确保顺序和 SQL 一致)
            cols = ['Order number', 'Order line no.', 'Date created', 'Shipping address first name',
                    'Shipping address last name', 'Shipping address street 1', 'Shipping address street 2',
                    'Shipping address country', 'Shipping address city', 'Shipping address state',
                    'Shipping address zip', 'Quantity', 'Offer SKU', 'Unit price', 'CostwayOrder']

            # 填充空值防止报错
            df = df.fillna('')
            data = [tuple(x) for x in df[cols].to_numpy()]
            print(data)

            # 6. 入库
            DBManager.insert_macy_orders(data)
            DBManager.update_costwaymacy_sku()
            return True, f"成功导入 {len(data)} 条 Macy 订单"

        except Exception as e:
            return False, str(e)

    @staticmethod
    def process_bestbuy_orders(file_path: str):
        try:
            df = pd.read_excel(file_path, dtype={"Shipping address zip": str})
            df.columns = [str(c).strip() for c in df.columns]

            required_cols = [
                "Order number",
                "Order line no.",
                "Date created",
                "Shipping address first name",
                "Shipping address street 1",
                "Shipping address country",
                "Shipping address city",
                "Shipping address state",
                "Shipping address zip",
                "Quantity",
                "Offer SKU",
                "Unit price",
            ]
            missing = [c for c in required_cols if c not in df.columns]
            if missing:
                return False, f"缺少列: {', '.join(missing)}"

            if "Date created" in df.columns:
                df["Date created"] = pd.to_datetime(df["Date created"], errors="coerce").dt.strftime("%Y-%m-%d")

            if "Shipping address street 2" not in df.columns:
                df["Shipping address street 2"] = ""

            # Use provided last name when present; only split first-name field if last name is empty.
            if "Shipping address last name" not in df.columns:
                df["Shipping address last name"] = ""

            def split_name(val):
                if val is None:
                    return "", ""
                parts = str(val).strip().split()
                if not parts:
                    return "", ""
                if len(parts) == 1:
                    return parts[0], ""
                return parts[0], " ".join(parts[1:])

            first_names = []
            last_names = []
            for first_val, last_val in zip(
                df["Shipping address first name"].tolist(),
                df["Shipping address last name"].tolist(),
            ):
                if last_val is None or str(last_val).strip() == "":
                    fn, ln = split_name(first_val)
                else:
                    fn = "" if first_val is None else str(first_val).strip()
                    ln = str(last_val).strip()
                first_names.append(fn)
                last_names.append(ln)

            df["Shipping address first name"] = first_names
            df["Shipping address last name"] = last_names

            df["Order number"] = df["Order number"].astype(str)
            counts = df.groupby("Order number").cumcount()
            mask = df.duplicated(subset=["Order number"], keep=False)
            if mask.any():
                df.loc[mask, "Order number"] += "-" + (counts[mask] + 1).astype(str)

            start_seq = DBManager.get_bestbuy_max_sequence()
            date_str = datetime.now().strftime("%y%m%d")
            df["CostwayOrder"] = [f"WHLHBB{date_str}-{start_seq + i + 1}" for i in range(len(df))]

            cols = [
                "Order number",
                "Order line no.",
                "Date created",
                "Shipping address first name",
                "Shipping address last name",
                "Shipping address street 1",
                "Shipping address street 2",
                "Shipping address country",
                "Shipping address city",
                "Shipping address state",
                "Shipping address zip",
                "Quantity",
                "Offer SKU",
                "Unit price",
                "CostwayOrder",
            ]

            df = df.fillna("")
            data = [tuple(x) for x in df[cols].to_numpy()]

            DBManager.insert_bestbuy_orders(data)
            DBManager.update_costwaybestbuy_sku()
            return True, f"成功导入 {len(data)} 条 Bestbuy 订单"

        except Exception as e:
            return False, str(e)

    @staticmethod
    def process_walmart_orders(file_path: str):
        try:
            df = pd.read_excel(file_path, dtype={"Zip": str})
            df.columns = [str(c).strip() for c in df.columns]

            col_map = {c.lower(): c for c in df.columns}

            def pick(*names):
                for n in names:
                    if n in col_map:
                        return col_map[n]
                return None

            po_col = pick("po#", "po number", "po_number")
            order_col = pick("order#", "order number", "order_number")
            date_col = pick("order date", "order_date")
            name_col = pick("customer name", "customer_name")
            addr1_col = pick("ship to address 1", "ship_to_address1")
            addr2_col = pick("ship to address 2", "ship_to_address2")
            country_col = pick("ship to country", "ship_to_country")
            city_col = pick("city")
            state_col = pick("state")
            zip_col = pick("zip")
            qty_col = pick("qty", "quantity")
            sku_col = pick("sku")
            cost_col = pick("item cost", "item_cost")

            required = {
                "PO#": po_col,
                "Order#": order_col,
                "Order Date": date_col,
                "Customer Name": name_col,
                "Ship to Address 1": addr1_col,
                "Ship to Country": country_col,
                "City": city_col,
                "State": state_col,
                "Zip": zip_col,
                "Qty": qty_col,
                "SKU": sku_col,
                "Item Cost": cost_col,
            }
            missing = [k for k, v in required.items() if v is None]
            if missing:
                return False, f"缺少列: {', '.join(missing)}"

            def split_name(val):
                if val is None:
                    return "", ""
                parts = str(val).strip().split()
                if not parts:
                    return "", ""
                if len(parts) == 1:
                    return parts[0], ""
                return parts[0], " ".join(parts[1:])

            first_names = []
            last_names = []
            for v in df[name_col].tolist():
                fn, ln = split_name(v)
                first_names.append(fn)
                last_names.append(ln)

            df_out = pd.DataFrame({
                "PO_Number": df[po_col],
                "Order_Number": df[order_col],
                "Order_Date": pd.to_datetime(df[date_col], errors="coerce").dt.strftime("%Y-%m-%d"),
                "First_Name": first_names,
                "Last_Name": last_names,
                "Ship_to_Address1": df[addr1_col],
                "Ship_to_Address2": df[addr2_col] if addr2_col else "",
                "Ship_to_Country": df[country_col],
                "City": df[city_col],
                "State": df[state_col],
                "Zip": df[zip_col],
                "Qty": df[qty_col],
                "SKU": df[sku_col],
                "Item_Cost": df[cost_col],
            })

            df_out["Order_Number"] = df_out["Order_Number"].astype(str)
            counts = df_out.groupby("Order_Number").cumcount()
            mask = df_out.duplicated(subset=["Order_Number"], keep=False)
            if mask.any():
                df_out.loc[mask, "Order_Number"] += "-" + (counts[mask] + 1).astype(str)

            start_seq = DBManager.get_walmart_max_sequence()
            date_str = datetime.now().strftime("%y%m%d")
            df_out["CostwayOrder"] = [f"WHLHWM{date_str}-{start_seq + i + 1}" for i in range(len(df_out))]

            cols = [
                "PO_Number",
                "Order_Number",
                "Order_Date",
                "First_Name",
                "Last_Name",
                "Ship_to_Address1",
                "Ship_to_Address2",
                "Ship_to_Country",
                "City",
                "State",
                "Zip",
                "Qty",
                "SKU",
                "Item_Cost",
                "CostwayOrder",
            ]

            df_out = df_out.fillna("")
            data = [tuple(x) for x in df_out[cols].to_numpy()]

            DBManager.insert_walmart_orders(data)
            DBManager.update_costwaywalmart_sku()
            return True, f"成功导入 {len(data)} 条 Walmart 订单"

        except Exception as e:
            return False, str(e)
    @staticmethod
    def process_lowes_orders(file_path: str):
        return False, "Lowes 导入逻辑未实现（先占位）"
