"""
Verify repricing_formula.py produces the same numbers as the Feishu Macy-kuyotq
Formula columns.

Pulls 20 live records from the Feishu bitable Macy-kuyotq-Mirakl
(tblfyStm2eu3hp1Q), runs our formulas on the inputs, and compares each
intermediate value against Feishu's own ROUND-trip result.

Run:
    python -m pytest tests/test_repricing_formula.py -v
or as a script:
    PYTHONIOENCODING=utf-8 python tests/test_repricing_formula.py
"""
import os
import sys
import json
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import requests

from app.services.repricing_formula import (
    cost_from_supplier_price,
    return_shipping_total,
    is_oversize_package,
    is_dim_surcharge,
    is_weight_surcharge,
    calculate_breakdown,
    realised_margin,
)


FEISHU_APP_ID = "cli_a940a2a1067adbd2"
FEISHU_APP_SECRET = "i2mKLGVzUDmu4v0U9HYEYdMGc0ZvZAgU"
APP_TOKEN = "QEeubiXYGa83zXs3Zt8cSSJPnih"
TABLE_ID = "tblfyStm2eu3hp1Q"

TOL = 0.02  # money rounding tolerance


def _token():
    r = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        timeout=15,
    )
    return r.json()["tenant_access_token"]


def _unwrap_formula(v):
    """Feishu formula fields come back as {'type': N, 'value': [..]}; unwrap to
    scalar. String-output Formula returns text segments like
    {'text': '是', 'type': 'text'} - unwrap those too.
    """
    if isinstance(v, dict):
        val = v.get("value", v)
        if isinstance(val, list):
            val = val[0] if val else None
        # text-segment shape
        if isinstance(val, dict) and "text" in val:
            return val["text"]
        return val
    return v


def _unwrap_text(v):
    """Text fields come back as [{'text': '...', 'type': 'text'}]; unwrap."""
    if isinstance(v, list) and v and isinstance(v[0], dict) and "text" in v[0]:
        return v[0]["text"]
    return v


def fetch_samples(n=20):
    token = _token()
    H = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/"
        f"{TABLE_ID}/records/search?page_size={n}"
    )
    body = {
        "field_names": [
            "供应商SKU", "供应商", "供应商价格",
            "长in", "宽in", "高in", "重LB",
            "退货运费(基础)", "活动折扣", "佣金比例",
            "成本", "退货运费(加附加费)", "退货成本预估（10%）(USD)",
            "公式计算出来的Price (USD)", "折扣后价格", "活动前原价",
            "利润", "利润率",
            "超大包裹", "Dim", "Weight",
        ]
    }
    r = requests.post(url, headers=H, json=body, timeout=30)
    items = (r.json().get("data") or {}).get("items") or []
    return items


def _approx(a, b, tol=TOL):
    if a is None or b is None:
        return False
    return abs(a - b) <= tol


def test_against_feishu_samples():
    """For each pulled record, reproduce every Feishu Formula column and check
    the discrepancy is within rounding tolerance.
    """
    samples = fetch_samples(20)
    assert samples, "could not fetch any samples from Feishu"

    skipped_no_input = 0
    skipped_no_supplier = 0
    checked = 0
    mismatches = []

    for rec in samples:
        f = rec.get("fields", {})

        sku = _unwrap_text(f.get("供应商SKU"))
        supplier = f.get("供应商")
        supplier_price = f.get("供应商价格")
        L = f.get("长in")
        W = f.get("宽in")
        H = f.get("高in")
        wt = f.get("重LB")
        return_base = f.get("退货运费(基础)")
        discount_factor = f.get("活动折扣")
        commission_rate = f.get("佣金比例")

        if any(v is None for v in (supplier_price, L, W, H, wt, return_base,
                                    discount_factor, commission_rate)):
            skipped_no_input += 1
            continue
        if supplier not in ("Costway", "Vevor"):
            skipped_no_supplier += 1
            continue

        fb_cost = _unwrap_formula(f.get("成本"))
        fb_rs_total = _unwrap_formula(f.get("退货运费(加附加费)"))
        fb_return_cost = _unwrap_formula(f.get("退货成本预估（10%）(USD)"))
        fb_formula_price = _unwrap_formula(f.get("公式计算出来的Price (USD)"))
        fb_discount_price = _unwrap_formula(f.get("折扣后价格"))
        fb_origin_price = _unwrap_formula(f.get("活动前原价"))
        fb_profit = _unwrap_formula(f.get("利润"))
        fb_margin = _unwrap_formula(f.get("利润率"))
        fb_oversize = _unwrap_formula(f.get("超大包裹"))
        fb_dim = _unwrap_formula(f.get("Dim"))
        fb_weight = _unwrap_formula(f.get("Weight"))

        bd = calculate_breakdown(
            supplier=supplier,
            supplier_price=supplier_price,
            return_shipping_base=return_base,
            discount_factor=discount_factor,
            length_in=L, width_in=W, height_in=H, weight_lb=wt,
        )

        # boolean parity
        if (fb_oversize == "是") != bd.is_oversize:
            mismatches.append((sku, "oversize", fb_oversize, bd.is_oversize))
        if (fb_dim == "是") != bd.is_dim:
            mismatches.append((sku, "dim", fb_dim, bd.is_dim))
        if (fb_weight == "是") != bd.is_weight:
            mismatches.append((sku, "weight", fb_weight, bd.is_weight))

        # numeric parity (use 0.02 tolerance for accumulated rounding diff)
        for name, ours, theirs in [
            ("cost", bd.cost, fb_cost),
            ("rs_total", bd.return_shipping_total, fb_rs_total),
            ("return_cost_est", bd.return_cost_estimate, fb_return_cost),
            ("formula_price", bd.formula_calc_price, fb_formula_price),
            ("discount_price", bd.discount_price, fb_discount_price),
            ("origin_price", bd.origin_price, fb_origin_price),
        ]:
            if not _approx(ours, theirs):
                mismatches.append((sku, name, theirs, ours))

        # check realised_margin against Feishu's 利润率
        m = realised_margin(
            current_origin_price=fb_origin_price,
            supplier=supplier, supplier_price=supplier_price,
            return_shipping_base=return_base,
            discount_factor=discount_factor,
            commission_rate=commission_rate,
            length_in=L, width_in=W, height_in=H, weight_lb=wt,
        )
        if fb_margin is not None and not _approx(m, fb_margin, tol=0.001):
            mismatches.append((sku, "margin", fb_margin, m))

        checked += 1

    print(f"\nChecked {checked} records (skipped {skipped_no_input} missing-input, "
          f"{skipped_no_supplier} bad-supplier)")

    if mismatches:
        print("\nMismatches:")
        for m in mismatches:
            print(f"  SKU={m[0]} field={m[1]} feishu={m[2]!r} ours={m[3]!r}")

    assert not mismatches, f"{len(mismatches)} formula mismatches"
    assert checked >= 5, "should check at least 5 records for confidence"


def test_supplier_cost_factors():
    """Spot-check the two supplier factor branches with manual numbers."""
    assert abs(cost_from_supplier_price(100.0, "Costway") - 75.0) < 1e-9
    assert abs(cost_from_supplier_price(100.0, "Vevor") - 89.88) < 1e-2


def test_unsupported_supplier_raises():
    import pytest
    with pytest.raises(ValueError):
        cost_from_supplier_price(100.0, "Songmics")


def test_surcharge_branches():
    """Make sure each surcharge boolean is reachable independently."""
    # small box - none of the three trip
    assert not is_oversize_package(10, 10, 10, 5)
    assert not is_dim_surcharge(10, 10, 10, 5)
    assert not is_weight_surcharge(10, 10, 10, 5)
    # > 96 longest side and under 150 lb -> oversize
    assert is_oversize_package(100, 10, 10, 50)
    # in dim range
    assert is_dim_surcharge(60, 20, 20, 40)
    # weight 50-150 lb
    assert is_weight_surcharge(10, 10, 10, 80)


def test_return_shipping_with_no_surcharges():
    """Small light package - only base shipping applies."""
    base = 15.0
    out = return_shipping_total(base, 12, 12, 12, 5)
    assert abs(out - base) < 1e-9


if __name__ == "__main__":
    import sys as _sys
    _sys.stdout.reconfigure(encoding="utf-8")
    print("=== test_against_feishu_samples ===")
    test_against_feishu_samples()
    print("\n=== test_supplier_cost_factors ===")
    test_supplier_cost_factors()
    print("OK")
    print("\n=== test_surcharge_branches ===")
    test_surcharge_branches()
    print("OK")
    print("\n=== test_return_shipping_with_no_surcharges ===")
    test_return_shipping_with_no_surcharges()
    print("OK")
    print("\nAll formula tests passed.")
