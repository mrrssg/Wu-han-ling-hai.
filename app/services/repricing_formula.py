"""
Pure-function pricing formulas for the macy_kuyotq automated repricing system.

These formulas mirror the Feishu bitable `Macy-kuyotq-Mirakl` (tblfyStm2eu3hp1Q)
computed columns. Verified against live Feishu samples in
tests/test_repricing_formula.py.

Every function here is pure (no I/O, no DB) so callers can unit-test and
batch-recompute without side effects.

Key invariants (Macy-kuyotq specific):
- Costway cost   = supplier_price * 0.75
- Vevor   cost   = supplier_price * 0.8 * 1.07 * 1.05
- discount_factor (e.g. 0.4)  and  commission_rate (e.g. 0.18) live in
  order_system.offer_pricing_config (snapshot of Feishu values).
- Origin price is the ANCHOR we push to Mirakl  (retail_prices.unit_origin_price).
- All money values written to Mirakl must be round(x, 2) - see
  feedback_mirakl_number_precision in memory.
"""
from dataclasses import dataclass
from typing import Optional


# ----- supplier cost factors (Macy-kuyotq ONLY; do not reuse for other stores) -----
COST_FACTOR_COSTWAY = 0.75
COST_FACTOR_VEVOR = 0.8 * 1.07 * 1.05  # = 0.8988

# ----- formula constants baked into the Feishu formula -----
RETURN_COST_RATIO = 0.10              # 退货成本预估 = 退货运费(加附加费) * 0.10
ROUNDED_OFFSET = 0.02                 # 折扣后价格 = ROUND(公式价格, 0) - 0.02
DIM_FACTOR_VOLUME_WEIGHT = 225        # 体积重 = L*W*H / 225

# ----- per-store "公式计算出来的Price" variants -----
# formula_price = (cost * cost_factor + return_cost_est) / divisor
#   macy_kuyotq : (成本 * 0.92 + 退货成本预估) / 0.6444
#   lowes_autool: (成本 * 1.0  + 退货成本预估) / 0.73   = 总成本 / 0.73
# The divisor is the implicit (1 - commission - target_margin); do not unfold.
PRICE_FORMULA_VARIANTS = {
    "macy": {"cost_factor": 0.92, "divisor": 0.6444},
    "lowes": {"cost_factor": 1.0, "divisor": 0.73},
}
DEFAULT_FORMULA_VARIANT = "macy"

# legacy aliases kept so any old import still resolves
PRICE_FORMULA_NUMERATOR_COST_FACTOR = PRICE_FORMULA_VARIANTS["macy"]["cost_factor"]
PRICE_FORMULA_DIVISOR = PRICE_FORMULA_VARIANTS["macy"]["divisor"]

# ----- return shipping surcharges (Lowes-style, used by Macy too) -----
SURCHARGE_OVERSIZE = 110
SURCHARGE_DIM = 28
SURCHARGE_WEIGHT = 28


# =============================================================================
# Building blocks
# =============================================================================

def cost_from_supplier_price(supplier_price: float, supplier: str) -> float:
    """成本 - cost after supplier-specific factor (NOT rounded; rounding happens
    at the boundary when writing to Mirakl).
    """
    if supplier == "Costway":
        return supplier_price * COST_FACTOR_COSTWAY
    if supplier == "Vevor":
        return supplier_price * COST_FACTOR_VEVOR
    raise ValueError(f"Unsupported supplier for macy_kuyotq cost calc: {supplier!r}")


def volume_weight(length_in: float, width_in: float, height_in: float) -> float:
    """体积重 (dimensional weight) = L * W * H / 225."""
    return length_in * width_in * height_in / DIM_FACTOR_VOLUME_WEIGHT


def girth(length_in: float, width_in: float, height_in: float) -> float:
    """最长边+腰围  = L + (W + H) * 2.
    Matches the Feishu formula literally (assumes the table puts the longest
    side in `长in`; that is also how the upstream pipeline records it).
    """
    return length_in + (width_in + height_in) * 2


def is_oversize_package(L: float, W: float, H: float, weight_lb: float) -> bool:
    """超大包裹 (UPS Large Package): big enough to count as oversize but still
    under the 150 lb cap.
    """
    vol_wt = volume_weight(L, W, H)
    max_dim = max(L, W, H)
    g = girth(L, W, H)
    max_wt = max(weight_lb, vol_wt)
    cond_dim = max_dim > 96 and max_wt < 150
    cond_girth = 126 < g <= 165 and max_wt < 150
    return cond_dim or cond_girth


def is_dim_surcharge(L: float, W: float, H: float, weight_lb: float) -> bool:
    """Dim surcharge (Additional Handling - Dim)."""
    max_dim = max(L, W, H)
    min_dim = min(L, W, H)
    g = girth(L, W, H)
    cond_a = (L + W + H - max_dim - min_dim) > 30
    cond_b = 48 < max_dim < 96
    cond_c = 105 < g <= 130
    return cond_a or cond_b or cond_c


def is_weight_surcharge(L: float, W: float, H: float, weight_lb: float) -> bool:
    """Weight surcharge: 50 < max(weight, vol_wt) < 150."""
    max_wt = max(weight_lb, volume_weight(L, W, H))
    return 50 < max_wt < 150


def return_shipping_total(
    return_shipping_base: float,
    length_in: float,
    width_in: float,
    height_in: float,
    weight_lb: float,
) -> float:
    """退货运费(加附加费) = base + (oversize ? 110 : 0) + (dim ? 28 : 0) + (weight ? 28 : 0)."""
    total = float(return_shipping_base)
    if is_oversize_package(length_in, width_in, height_in, weight_lb):
        total += SURCHARGE_OVERSIZE
    if is_dim_surcharge(length_in, width_in, height_in, weight_lb):
        total += SURCHARGE_DIM
    if is_weight_surcharge(length_in, width_in, height_in, weight_lb):
        total += SURCHARGE_WEIGHT
    return total


# =============================================================================
# Forward: cost + config -> origin_price (used for triggered repricing)
# =============================================================================

@dataclass
class PriceBreakdown:
    """Full audit trail of one price calculation; written to
    offer_price_change_log.
    """
    cost: float
    return_shipping_extra: float
    return_shipping_total: float
    return_cost_estimate: float
    total_cost: float
    formula_calc_price: float
    discount_price: float                  # 折扣后价格 (ROUND(formula, 0) - 0.02)
    origin_price: float                    # 活动前原价 (= discount_price / discount_factor)
    is_oversize: bool
    is_dim: bool
    is_weight: bool


def calculate_breakdown(
    *,
    supplier: str,
    supplier_price: float,
    return_shipping_base: float,
    discount_factor: float,
    length_in: float,
    width_in: float,
    height_in: float,
    weight_lb: float,
    formula_variant: str = DEFAULT_FORMULA_VARIANT,
) -> PriceBreakdown:
    """Compute every intermediate value used by the Feishu formula chain.
    Returns a structured breakdown so callers can log every number to
    offer_price_change_log.

    formula_variant selects the "公式计算出来的Price" step:
      - 'macy'  : (cost * 0.92 + return_cost_est) / 0.6444
      - 'lowes' : (cost * 1.0  + return_cost_est) / 0.73   (= 总成本 / 0.73)
    Everything else (surcharges, discount_price, origin_price) is identical.
    """
    variant = PRICE_FORMULA_VARIANTS.get(formula_variant)
    if variant is None:
        raise ValueError(f"unknown formula_variant: {formula_variant!r}")

    cost = cost_from_supplier_price(supplier_price, supplier)

    extra = 0.0
    is_os = is_oversize_package(length_in, width_in, height_in, weight_lb)
    is_d = is_dim_surcharge(length_in, width_in, height_in, weight_lb)
    is_w = is_weight_surcharge(length_in, width_in, height_in, weight_lb)
    if is_os:
        extra += SURCHARGE_OVERSIZE
    if is_d:
        extra += SURCHARGE_DIM
    if is_w:
        extra += SURCHARGE_WEIGHT

    rs_total = return_shipping_base + extra
    return_cost_est = rs_total * RETURN_COST_RATIO
    total_cost = cost + return_cost_est

    formula_price = (
        cost * variant["cost_factor"] + return_cost_est
    ) / variant["divisor"]
    discount_price = round(formula_price, 0) - ROUNDED_OFFSET
    origin_price = discount_price / discount_factor

    return PriceBreakdown(
        cost=cost,
        return_shipping_extra=extra,
        return_shipping_total=rs_total,
        return_cost_estimate=return_cost_est,
        total_cost=total_cost,
        formula_calc_price=formula_price,
        discount_price=discount_price,
        origin_price=origin_price,
        is_oversize=is_os,
        is_dim=is_d,
        is_weight=is_w,
    )


# =============================================================================
# Reverse: existing origin_price + current cost -> realised margin
# (used to check the < 5% trigger)
# =============================================================================

def realised_margin(
    *,
    current_origin_price: float,
    supplier: str,
    supplier_price: float,
    return_shipping_base: float,
    discount_factor: float,
    commission_rate: float,
    length_in: float,
    width_in: float,
    height_in: float,
    weight_lb: float,
) -> float:
    """Compute the CURRENT profit margin if we sold at the existing origin_price
    while paying the LATEST supplier cost. This is what the trigger checks.

    Formula:
        discount_price = origin_price * discount_factor
        to_us         = discount_price * (1 - commission_rate)
        profit        = to_us - total_cost
        margin        = profit / discount_price

    where total_cost = supplier_cost + return_cost_estimate, with the same
    surcharge logic as calculate_breakdown.
    """
    cost = cost_from_supplier_price(supplier_price, supplier)
    rs_total = return_shipping_total(
        return_shipping_base, length_in, width_in, height_in, weight_lb
    )
    return_cost_est = rs_total * RETURN_COST_RATIO
    total_cost = cost + return_cost_est

    discount_price = current_origin_price * discount_factor
    to_us = discount_price * (1 - commission_rate)
    profit = to_us - total_cost
    if discount_price == 0:
        return 0.0
    return profit / discount_price


# =============================================================================
# Threshold helpers
# =============================================================================

def cost_volatility_exceeds(
    old_cost: Optional[float], new_cost: float, threshold: float = 0.30
) -> bool:
    """Return True if |new-old|/old > threshold. If old is None or 0, False
    (no baseline -> cannot judge; caller decides whether to skip).
    """
    if old_cost is None or old_cost == 0:
        return False
    return abs(new_cost - old_cost) / old_cost > threshold


def price_volatility_exceeds(
    old_price: Optional[float], new_price: float, threshold: float = 0.30
) -> bool:
    """Symmetric helper for sanity checks on origin price moves."""
    if old_price is None or old_price == 0:
        return False
    return abs(new_price - old_price) / old_price > threshold
