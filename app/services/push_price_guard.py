# -*- coding: utf-8 -*-
"""推价校验（2026-07-16错价事故后建，用户定案）：

只校验、绝不改价——公式是铁律，校验失败=拦下不推+说明原因，让人查数据。
三段防线：
  ① 推送前硬校验 validate_push_price()：毛利自洽/保本线/原价折扣关系/数据新鲜度
  ② 推送前软提醒：与Mirakl现价偏离过大→候选页展示（"变动"列本来就有）
  ③ 推送后回读验证 scripts/push_verify_daily.py：夜间同步后比对Mirakl真实价/状态,
     不一致或offer被平台停用→issue_log(price_push_mismatch)→首页待办报警
"""
from datetime import datetime, timedelta
from typing import Dict, List, Optional

MARGIN_TOLERANCE = 0.02        # 推送价毛利偏离目标档超过±2个点=数据有问题
SUPPLIER_PRICE_MAX_AGE_D = 7   # 供应商价格超7天没更新不许用
COST_JUMP_ALERT = 0.30         # 成本较上次快照突变超30%=先查供应商表


def validate_push_price(*, target_origin: float, target_discount: float,
                        cost: float, return_cost_estimate: float,
                        commission_rate: float, discount_factor: float,
                        nominal_margin: float,
                        supplier_updated_at: Optional[datetime] = None,
                        last_cost_snapshot: Optional[float] = None) -> List[str]:
    """返回错误列表；空列表=通过。只判断，不修改任何价格。"""
    errors: List[str] = []

    if not target_discount or target_discount <= 0:
        return ["目标折扣价为空/非正数"]

    # ① 毛利自洽：按推送价反算毛利，必须落在目标档±2点内
    m = (target_discount * (1 - commission_rate) - cost - return_cost_estimate) / target_discount
    if abs(m - nominal_margin) > MARGIN_TOLERANCE:
        errors.append(
            f"毛利自洽失败：推送价反算毛利{m*100:.1f}%，偏离目标档{nominal_margin*100:.0f}%"
            f"超过{MARGIN_TOLERANCE*100:.0f}个点——价格或成本数据有问题，先查再推")

    # ② 保本线：扣佣后必须盖住成本（任何档位都不许亏本卖）
    if target_discount * (1 - commission_rate) < cost:
        errors.append(
            f"保本校验失败：折扣价${target_discount:.2f}扣佣后"
            f"${target_discount*(1-commission_rate):.2f} < 成本${cost:.2f}——亏本价禁止推送")

    # ③ 原价/折扣关系：原价必须=折扣价÷折扣系数（2位精度）
    if abs(target_origin - round(target_discount / discount_factor, 2)) > 0.02:
        errors.append(
            f"价格关系失败：原价${target_origin:.2f} ≠ 折扣价${target_discount:.2f}÷{discount_factor}")

    # ④ 供应商价格新鲜度
    if supplier_updated_at is not None:
        age = (datetime.now() - supplier_updated_at).days
        if age > SUPPLIER_PRICE_MAX_AGE_D:
            errors.append(f"供应商价格已{age}天没更新（>7天）——先刷新供应商表再推")

    # ⑤ 成本突变
    if last_cost_snapshot and last_cost_snapshot > 0 and cost > 0:
        jump = abs(cost - last_cost_snapshot) / last_cost_snapshot
        if jump > COST_JUMP_ALERT:
            errors.append(
                f"成本突变{jump*100:.0f}%（快照${last_cost_snapshot:.2f}→${cost:.2f}）"
                f"——可能供应商表数据错误，人工核实后再推")

    return errors
