# -*- coding: utf-8 -*-
"""每周更新 FedEx Ground 燃油率 = f(EIA 全美on-highway柴油周价)。

FedEx Ground/Home Delivery 燃油率由全美 No.2 柴油零售周价套档位公式得出
(Effective 2026-06-01,抓自 fedex.com/en-us/shipping/fuel-surcharge.html):
  柴油 <  $4.99: 每 +$0.27 → 燃油 +0.25%,锚点 $4.45 = 25.00%
  柴油 >= $4.99: 每 +$0.09 → 燃油 +0.25%,锚点 $4.99 = 25.50%
滞后:FedEx 某周(周一起)的率 = f(该周一往前 1 周的 EIA 柴油价)。

数据源:EIA 周柴油 xls(服务器可直连,无需 API key)。已验证对上 FedEx 官方:
  EIA 7/20 $5.134 → 25.75%(FedEx 7/27-8/2);EIA 7/13 $4.796 → 25.25%(FedEx 7/20-26)。
写 instance/fedex/fuel_rate.json。每周一 cron 跑。抓不到/异常则保留旧值不覆盖。
"""
import datetime as dt
import json
import sys
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path

import requests
import xlrd

EIA_URL = "https://www.eia.gov/dnav/pet/hist_xls/EMD_EPD2D_PTE_NUS_DPGw.xls"
OUT = Path(__file__).resolve().parents[1] / "instance" / "fedex" / "fuel_rate.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64) Chrome/120 Safari/537.36"


def fedex_ground_fuel_pct(diesel):
    """柴油价($/gal) → FedEx Ground 燃油率(%)。用 Decimal 防边界浮点误差。"""
    d = Decimal(str(diesel))
    if d < Decimal("4.99"):
        steps = int(((d - Decimal("4.45")) / Decimal("0.27")).to_integral_value(rounding=ROUND_FLOOR))
        rate = Decimal("25.00") + Decimal("0.25") * steps
    else:
        steps = int(((d - Decimal("4.99")) / Decimal("0.09")).to_integral_value(rounding=ROUND_FLOOR))
        rate = Decimal("25.50") + Decimal("0.25") * steps
    return float(rate)


def fetch_eia_series():
    r = requests.get(EIA_URL, headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    wb = xlrd.open_workbook(file_contents=r.content)
    sh = wb.sheet_by_name("Data 1")
    series = {}
    for row in range(3, sh.nrows):
        try:
            d = xlrd.xldate_as_datetime(sh.cell_value(row, 0), wb.datemode).date()
            v = float(sh.cell_value(row, 1))
        except Exception:
            continue
        if v > 0:
            series[d] = v
    return series


def main():
    today = dt.date.today()
    monday = today - dt.timedelta(days=today.weekday())   # 本 FedEx 周的周一
    target = monday - dt.timedelta(days=7)                # 驱动本周率的 EIA 日期(往前1周)
    try:
        series = fetch_eia_series()
    except Exception as exc:
        print(f"[fedex_fuel] EIA 抓取失败,保留旧值不覆盖: {exc}")
        return 1
    if not series:
        print("[fedex_fuel] EIA 无数据,不更新")
        return 1
    dates = sorted(d for d in series if d <= target) or sorted(series)
    eia_date = dates[-1]
    diesel = series[eia_date]
    rate = fedex_ground_fuel_pct(diesel)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "rate": rate,
        "updated": today.isoformat(),
        "diesel_price": diesel,
        "diesel_date": eia_date.isoformat(),
        "source": "EIA weekly diesel + FedEx Ground formula",
    }, ensure_ascii=False), encoding="utf-8")
    print(f"[fedex_fuel] OK 本周一={monday} 用EIA {eia_date} 柴油${diesel} → 燃油 {rate}% (已写 {OUT})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
