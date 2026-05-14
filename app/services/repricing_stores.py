"""
Single source of truth for the repricing system's per-store configuration.

Every repricing service (offer sync, Feishu config sync, OF24 push, monitor,
full export, web routes) imports from here so adding a new store is a
one-place change.

mode:
  - 'non_dropship' : Mirakl marketplace where `price` IS the customer-facing
                     price (Macy-kuyotq). OF52 export carries the customer
                     price at prices[0].origin_price; OF24/Excel write the
                     single `price` field.
  - 'dropship'     : Mirakl Dropship marketplace where `price` is the
                     wholesale cost and the customer-facing price lives in
                     retail_prices / the `retail-price` Excel column
                     (Lowes-Autool). We only ever touch the retail side.

formula_variant:  selects the "公式计算出来的Price" step in repricing_formula
                  ('macy' = (cost*0.92+rc)/0.6444, 'lowes' = (cost+rc)/0.73).

excel_template:   filename under instance/repricing/ used as the styled base
                  for Part 2 export.
"""
from typing import Dict, List, Optional


FEISHU_APP_TOKEN = "QEeubiXYGa83zXs3Zt8cSSJPnih"


REPRICING_STORES: Dict[str, Dict] = {
    "macy_kuyotq": {
        "label": "Macy-Kuyotq",
        "platform": "Macy",            # offerprice_listing.platform
        "shop_name": "kuyotq",         # offerprice_listing.shop_name
        "mode": "non_dropship",
        "formula_variant": "macy",
        "feishu_app_token": FEISHU_APP_TOKEN,
        "feishu_table_id": "tblfyStm2eu3hp1Q",   # Macy-kuyotq-Mirakl
        "feishu_label": "Macy-kuyotq-Mirakl",
        "excel_template": "offers_import_blank.xlsx",
    },
    "lowes_autool": {
        "label": "Lowes-Autool",
        "platform": "Lowes",
        "shop_name": "autool",
        "mode": "dropship",
        "formula_variant": "lowes",
        "feishu_app_token": FEISHU_APP_TOKEN,
        "feishu_table_id": "tblGp3uvtOe99vjY",   # Lowes-Autool-Mirakl
        "feishu_label": "Lowes-Autool-Mirakl",
        "excel_template": "offers_import_lowes_blank.xlsx",
    },
}


def get_store(store_key: str) -> Dict:
    cfg = REPRICING_STORES.get(store_key)
    if not cfg:
        raise ValueError(f"unsupported repricing store: {store_key!r}")
    return cfg


def is_supported(store_key: str) -> bool:
    return store_key in REPRICING_STORES


def all_store_keys() -> List[str]:
    return list(REPRICING_STORES.keys())


def store_options() -> Dict[str, str]:
    """{store_key: label} for UI dropdowns."""
    return {k: v["label"] for k, v in REPRICING_STORES.items()}
