#!/usr/bin/env python3
"""Calculate a 2026 U.S. FedEx Ground return shipment estimate."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable


CENT = Decimal("0.01")
FOUR_DECIMALS = Decimal("0.0001")
ONE = Decimal("1")
DIM_DIVISOR = Decimal("139")


def dec(value: str | int | float | Decimal) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def ceil_decimal(value: Decimal) -> int:
    return int(value.quantize(ONE, rounding=ROUND_CEILING))


@dataclass
class Charge:
    name: str
    amount: Decimal
    fuel_eligible: bool = False


def load_rates(path: Path) -> dict[int, dict[int, Decimal]]:
    rates: dict[int, dict[int, Decimal]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        expected = {"weight", "zone2", "zone3", "zone4", "zone5", "zone6", "zone7", "zone8"}
        if set(reader.fieldnames or []) != expected:
            raise ValueError(f"Unexpected rate-table columns: {reader.fieldnames}")
        for row in reader:
            weight = int(row["weight"])
            rates[weight] = {
                zone: dec(row[f"zone{zone}"])
                for zone in range(2, 9)
            }
    missing = sorted(set(range(1, 151)) - set(rates))
    if missing:
        raise ValueError(f"Rate table is missing weights: {missing}")
    return rates


def zone_band(zone: int) -> str:
    if zone == 2:
        return "2"
    if zone in (3, 4):
        return "3-4"
    if zone in (5, 6):
        return "5-6"
    return "7+"


AHS = {
    "dimension": {"2": "29.50", "3-4": "32.75", "5-6": "38.50", "7+": "40.75"},
    "weight": {"2": "46.00", "3-4": "50.25", "5-6": "56.25", "7+": "58.75"},
    "packaging": {"2": "26.50", "3-4": "30.75", "5-6": "33.00", "7+": "33.75"},
}
OVERSIZE = {"2": "255", "3-4": "275", "5-6": "320", "7+": "330"}
DELIVERY_AREA = {
    "none": "0",
    "commercial": "4.45",
    "extended-commercial": "5.55",
    "residential": "6.60",
    "extended-residential": "8.80",
    "remote": "16.75",
    "alaska": "46",
    "hawaii": "16.25",
    "intra-hawaii": "1.10",
}
RETURN_METHOD = {
    "none": ("", "0", False),
    "print-label": ("FedEx Print Return Label", "1.05", False),
    "email-label": ("FedEx Email Return Label", "1.05", False),
    "calltag-commercial": ("FedEx Ground Call Tag - Commercial", "8.80", True),
    "calltag-residential": ("FedEx Ground Call Tag - Residential", "10", True),
}
SIGNATURE = {
    "none": ("", "0"),
    "indirect": ("Indirect Signature Required", "7.60"),
    "direct": ("Direct Signature Required", "7.60"),
    "adult": ("Adult Signature Required", "10"),
}


def declared_value_fee(value: Decimal | None) -> Decimal:
    if value is None or value <= 100:
        return Decimal("0")
    if value <= 300:
        return Decimal("4.95")
    hundreds = ceil_decimal(value / Decimal("100"))
    return money(Decimal(hundreds) * Decimal("1.65"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Calculate and explain a 2026 U.S. FedEx Ground return estimate."
    )
    parser.add_argument("--length", required=True, type=Decimal)
    parser.add_argument("--width", required=True, type=Decimal)
    parser.add_argument("--height", required=True, type=Decimal)
    parser.add_argument(
        "--actual-weight",
        type=Decimal,
        help="Physical scale weight in pounds. Omit when unknown; never pass rated weight here.",
    )
    parser.add_argument("--zone", required=True, type=int, choices=range(2, 9))
    parser.add_argument("--origin-zip")
    parser.add_argument("--destination-zip", default="92337")
    parser.add_argument(
        "--fuel-rate",
        required=True,
        type=Decimal,
        help="Weekly FedEx Ground fuel percentage, e.g. 25.75.",
    )
    parser.add_argument("--discount", type=Decimal, default=Decimal("30"))
    parser.add_argument(
        "--billing",
        choices=("sender", "recipient", "third-party"),
        default="sender",
    )
    parser.add_argument(
        "--return-method",
        choices=tuple(RETURN_METHOD),
        default="none",
    )
    parser.add_argument("--residential-destination", action="store_true")
    parser.add_argument(
        "--delivery-area",
        choices=tuple(DELIVERY_AREA),
        default="none",
    )
    parser.add_argument("--packaging-ahs", action="store_true")
    parser.add_argument("--signature", choices=tuple(SIGNATURE), default="none")
    parser.add_argument("--address-correction", action="store_true")
    parser.add_argument("--reroute", action="store_true")
    parser.add_argument("--payer-rebilling", action="store_true")
    parser.add_argument("--declared-value", type=Decimal)
    parser.add_argument("--demand-surcharge", type=Decimal, default=Decimal("0"))
    parser.add_argument("--other-fuel-eligible", type=Decimal, default=Decimal("0"))
    parser.add_argument("--other-nonfuel-fees", type=Decimal, default=Decimal("0"))
    parser.add_argument(
        "--rates",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "references" / "ground-rates-2026.csv",
    )
    parser.add_argument("--json", action="store_true")
    return parser


def calculate(args: argparse.Namespace) -> dict:
    rounded_sides = sorted(
        [ceil_decimal(args.length), ceil_decimal(args.width), ceil_decimal(args.height)],
        reverse=True,
    )
    longest, second, shortest = rounded_sides
    volume = longest * second * shortest
    length_girth = longest + (2 * second) + (2 * shortest)
    dim_raw = Decimal(volume) / DIM_DIVISOR
    dim_weight = ceil_decimal(dim_raw)
    actual_rounded = (
        ceil_decimal(args.actual_weight) if args.actual_weight is not None else None
    )
    chargeable_weight = max(dim_weight, actual_rounded or 0)
    rates = load_rates(args.rates)

    if chargeable_weight <= 150:
        published_base = rates[chargeable_weight][args.zone]
        base_method = f"2026 list rate at {chargeable_weight} lb"
    else:
        per_pound_rate = (rates[150][args.zone] / Decimal("150")).quantize(
            FOUR_DECIMALS, rounding=ROUND_HALF_UP
        )
        published_base = money(per_pound_rate * Decimal(chargeable_weight))
        base_method = (
            f"prorated from 150-lb rate ${rates[150][args.zone]} / 150"
            f" = ${per_pound_rate}/lb × {chargeable_weight}"
        )

    discount = money(published_base * (args.discount / Decimal("100")))
    net_transport = money(published_base - discount)
    charges: list[Charge] = [
        Charge("Transportation Charge", money(published_base), True),
        Charge("Discount", -discount, True),
    ]

    unauthorized_reasons: list[str] = []
    if args.actual_weight is not None and args.actual_weight > 150:
        unauthorized_reasons.append("actual weight > 150 lb")
    if longest > 108:
        unauthorized_reasons.append("longest side > 108 in")
    if length_girth > 165:
        unauthorized_reasons.append("length and girth > 165 in")

    oversize_reasons: list[str] = []
    if longest > 96:
        oversize_reasons.append("longest side > 96 in")
    if length_girth > 130:
        oversize_reasons.append("length and girth > 130 in")
    if volume > 17280:
        oversize_reasons.append("volume > 17,280 in³")
    if args.actual_weight is not None and args.actual_weight > 110:
        oversize_reasons.append("actual weight > 110 lb")

    ahs_reasons: dict[str, list[str]] = {
        "dimension": [],
        "weight": [],
        "packaging": [],
    }
    if longest > 48:
        ahs_reasons["dimension"].append("longest side > 48 in")
    if second > 30:
        ahs_reasons["dimension"].append("second-longest side > 30 in")
    if length_girth > 105:
        ahs_reasons["dimension"].append("length and girth > 105 in")
    if volume > 10368:
        ahs_reasons["dimension"].append("volume > 10,368 in³")
    if args.actual_weight is not None and args.actual_weight > 50:
        ahs_reasons["weight"].append("actual weight > 50 lb")
    if args.packaging_ahs:
        ahs_reasons["packaging"].append("non-standard packaging flag")

    band = zone_band(args.zone)
    classification = "standard"
    classification_reasons: list[str] = []
    if unauthorized_reasons:
        classification = "Ground Unauthorized"
        classification_reasons = unauthorized_reasons
        charges.append(Charge("Ground Unauthorized Package Charge", Decimal("1875"), True))
    elif oversize_reasons:
        classification = "Oversize"
        classification_reasons = oversize_reasons
        charges.append(Charge("Oversize Charge", dec(OVERSIZE[band]), True))
    else:
        applicable_ahs = [
            (dec(AHS[category][band]), category)
            for category, reasons in ahs_reasons.items()
            if reasons
        ]
        if applicable_ahs:
            amount, category = max(applicable_ahs)
            classification = f"Additional Handling - {category.title()}"
            classification_reasons = ahs_reasons[category]
            charges.append(
                Charge(f"Additional Handling - {category.title()}", amount, True)
            )

    return_name, return_amount, return_fuel = RETURN_METHOD[args.return_method]
    if return_name:
        charges.append(Charge(return_name, dec(return_amount), return_fuel))
    if args.residential_destination:
        charges.append(Charge("Residential Delivery Charge", Decimal("6.45"), True))
    delivery_area_amount = dec(DELIVERY_AREA[args.delivery_area])
    if delivery_area_amount:
        charges.append(
            Charge(
                f"Delivery Area Surcharge - {args.delivery_area}",
                delivery_area_amount,
                True,
            )
        )
    signature_name, signature_amount = SIGNATURE[args.signature]
    if signature_name:
        charges.append(Charge(signature_name, dec(signature_amount), True))
    if args.address_correction:
        charges.append(Charge("Address Correction", Decimal("25.50"), True))
    if args.reroute:
        charges.append(Charge("Reroute", Decimal("25.50"), True))
    if args.payer_rebilling:
        charges.append(Charge("Payer Rebilling", Decimal("25.50"), False))
    declared_fee = declared_value_fee(args.declared_value)
    if declared_fee:
        charges.append(Charge("Declared Value", declared_fee, False))
    if args.demand_surcharge:
        charges.append(Charge("Demand Surcharge", money(args.demand_surcharge), True))
    if args.other_fuel_eligible:
        charges.append(
            Charge("Other Fuel-Eligible Surcharges", money(args.other_fuel_eligible), True)
        )
    if args.other_nonfuel_fees:
        charges.append(
            Charge("Other Non-Fuel Fees", money(args.other_nonfuel_fees), False)
        )

    # Transportation and Discount are both fuel-eligible, so their sum is net transportation.
    fuel_base = money(sum((item.amount for item in charges if item.fuel_eligible), Decimal("0")))
    fuel = money(fuel_base * (args.fuel_rate / Decimal("100")))
    charges.append(Charge("Fuel Surcharge", fuel, False))

    pre_third_party = money(sum((item.amount for item in charges), Decimal("0")))
    third_party = Decimal("0")
    if args.billing == "third-party":
        third_party = money(pre_third_party * Decimal("0.05"))
        charges.append(Charge("Third Party Billing", third_party, False))

    total = money(sum((item.amount for item in charges), Decimal("0")))
    warnings: list[str] = []
    if args.actual_weight is None:
        warnings.append(
            "Actual scale weight is unknown. Weight-based AHS, Oversize, and Ground "
            "Unauthorized triggers were not evaluated."
        )
    if classification == "Ground Unauthorized":
        warnings.append(
            "FedEx may refuse, return, or dispose of a Ground Unauthorized package."
        )

    return {
        "inputs": {
            "origin_zip": args.origin_zip,
            "destination_zip": args.destination_zip,
            "rounded_dimensions_in": rounded_sides,
            "actual_weight_lb": str(args.actual_weight) if args.actual_weight is not None else None,
            "zone": args.zone,
            "fuel_rate_percent": str(args.fuel_rate),
            "transport_discount_percent": str(args.discount),
            "billing": args.billing,
        },
        "weight": {
            "volume_in3": volume,
            "length_and_girth_in": length_girth,
            "dimensional_weight_raw_lb": str(dim_raw.quantize(Decimal("0.0001"))),
            "dimensional_weight_lb": dim_weight,
            "actual_rounded_lb": actual_rounded,
            "chargeable_weight_lb": chargeable_weight,
        },
        "classification": {
            "name": classification,
            "reasons": classification_reasons,
            "all_ahs_reasons": ahs_reasons,
            "all_oversize_reasons": oversize_reasons,
            "all_unauthorized_reasons": unauthorized_reasons,
        },
        "base_rate": {
            "method": base_method,
            "published": str(money(published_base)),
            "discount": str(discount),
            "net": str(net_transport),
        },
        "fuel_base": str(fuel_base),
        "pre_third_party_total": str(pre_third_party),
        "charges": [
            {"name": item.name, "amount": str(money(item.amount))}
            for item in charges
        ],
        "total": str(total),
        "warnings": warnings,
    }


def print_text(result: dict) -> None:
    weight = result["weight"]
    classification = result["classification"]
    print(
        f"Dimensions: {result['inputs']['rounded_dimensions_in']} in | "
        f"Volume: {weight['volume_in3']} in³ | "
        f"Length+girth: {weight['length_and_girth_in']} in"
    )
    print(
        f"Actual rounded: {weight['actual_rounded_lb']} lb | "
        f"Dimensional: {weight['dimensional_weight_lb']} lb | "
        f"Chargeable: {weight['chargeable_weight_lb']} lb"
    )
    print(
        f"Classification: {classification['name']} "
        f"({'; '.join(classification['reasons']) or 'no trigger'})"
    )
    print(f"Base method: {result['base_rate']['method']}")
    print("\nCharges")
    for item in result["charges"]:
        print(f"  {item['name']:<42} ${Decimal(item['amount']):>10.2f}")
    print(f"  {'TOTAL':<42} ${Decimal(result['total']):>10.2f}")
    for warning in result["warnings"]:
        print(f"WARNING: {warning}")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args()
    result = calculate(args)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print_text(result)


if __name__ == "__main__":
    main()
