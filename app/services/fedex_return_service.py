# -*- coding: utf-8 -*-
"""FedEx Ground 退货运费测算 —— 编排层。

流程：用户只给 始发ZIP + 尺寸 + 重量 → 本地查 ZIP→zone 表(FedEx官方精确zone，
到固定目的地退货仓 92337)→ 调 fedex_ground_calc 引擎算逐行费用。

数据文件(instance/fedex/)：
  - zone_92337.csv      : zip3 → Ground zone(2-8;1=AK/9=HI 标特殊)。浏览器抓FedEx Find Zones一次性建，基本永不变。
  - ground-rates-2026.csv: 2026 U.S. Ground 费率表(重量1-150 × zone2-8)。
  - fuel_rate.json       : 当前周燃油率%(每周更新一次)。
折扣固定30%，目的地固定92337(用户2026-07-29定)。
"""
import csv
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from app.services import fedex_ground_calc

_INST = Path(__file__).resolve().parents[1].parent / "instance" / "fedex"
_ZONE_CSV = _INST / "zone_92337.csv"
_RATES_CSV = _INST / "ground-rates-2026.csv"
_FUEL_JSON = _INST / "fuel_rate.json"

DEST_ZIP = "92337"
DEFAULT_DISCOUNT = Decimal("30")   # 用户FedEx账户运输折扣
DEFAULT_FUEL = Decimal("25.0")     # 兜底(当前值，实际读 fuel_rate.json)

_zone_cache = None


def _load_zones():
    global _zone_cache
    if _zone_cache is None:
        d = {}
        with open(_ZONE_CSV, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                d[str(row["zip3"]).zfill(3)] = int(row["zone"])
        _zone_cache = d
    return _zone_cache


def zone_for_zip(origin_zip):
    """始发ZIP → (zone, note)。zone为None表示查不到；1=AK/9=HI 标特殊(费率表不覆盖)。"""
    digits = "".join(ch for ch in (origin_zip or "") if ch.isdigit())
    if len(digits) < 3:
        return None, "ZIP 格式无效(至少要前3位)"
    z3 = digits[:3]
    zone = _load_zones().get(z3)
    if zone is None:
        return None, f"ZIP 前缀 {z3} 未分配 FedEx zone(NA/波多黎各等)"
    if zone == 1:
        return 1, f"ZIP {z3}xx = 阿拉斯加(FedEx zone 1)，标准 Ground 费率表不覆盖，走特殊费率"
    if zone == 9:
        return 9, f"ZIP {z3}xx = 夏威夷(FedEx zone 9)，标准 Ground 费率表不覆盖，走特殊费率"
    return zone, None


def get_fuel_rate():
    """返回 (Decimal 燃油率%, updated 字符串或None)。"""
    try:
        d = json.loads(_FUEL_JSON.read_text(encoding="utf-8"))
        return Decimal(str(d["rate"])), d.get("updated")
    except Exception:
        return DEFAULT_FUEL, None


def set_fuel_rate(rate, updated):
    _INST.mkdir(parents=True, exist_ok=True)
    _FUEL_JSON.write_text(
        json.dumps({"rate": float(rate), "updated": updated}, ensure_ascii=False),
        encoding="utf-8",
    )


def _d(v, default=None):
    if v in (None, "", "None"):
        return default
    return Decimal(str(v))


def estimate(origin_zip, length, width, height, actual_weight=None, *,
             discount=None, return_method="none", residential=False,
             signature="none", packaging_ahs=False, delivery_area="none",
             billing="sender", declared_value=None):
    """返回结果 dict：ok / zone / fuel_rate / 以及引擎的逐行费用(charges/total/...)。"""
    zone, znote = zone_for_zip(origin_zip)
    fuel, fuel_updated = get_fuel_rate()
    disc = _d(discount, DEFAULT_DISCOUNT)

    base = {
        "origin_zip": origin_zip, "destination_zip": DEST_ZIP,
        "zone": zone, "zone_note": znote,
        "fuel_rate": str(fuel), "fuel_updated": fuel_updated,
        "discount": str(disc),
    }
    if zone is None or zone in (1, 9):
        base["ok"] = False
        base["msg"] = znote or "查不到 zone"
        return base

    args = SimpleNamespace(
        length=_d(length), width=_d(width), height=_d(height),
        actual_weight=_d(actual_weight),
        zone=int(zone),
        origin_zip=origin_zip, destination_zip=DEST_ZIP,
        fuel_rate=fuel, discount=disc,
        rates=_RATES_CSV,
        billing=(billing or "sender"),
        return_method=(return_method or "none"),
        residential_destination=bool(residential),
        delivery_area=(delivery_area or "none"),
        packaging_ahs=bool(packaging_ahs),
        signature=(signature or "none"),
        address_correction=False,
        reroute=False,
        payer_rebilling=False,
        declared_value=_d(declared_value),
        demand_surcharge=Decimal("0"),
        other_fuel_eligible=Decimal("0"),
        other_nonfuel_fees=Decimal("0"),
    )
    result = fedex_ground_calc.calculate(args)
    result.update(base)
    result["ok"] = True
    return result
