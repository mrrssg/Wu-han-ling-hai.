# 数据库速查手册（run_sql 工具专用）

只许SELECT。常用库：order_system（业务）、autooperate（供应商价）。日期条件用 DATE/DATE_SUB(CURDATE(),INTERVAL n DAY)。

## order_system.pricing_tier —— 分档定价结果（每天06:30重算）
store_key('lowes_autool') | shop_sku | tier('tier_12/tier_15/tier_18/delist/cold_watch/cold_12') |
target_margin(0.12/0.15/0.18) | reason_text(为什么进这一档) | evidence_json(全部证据数字) |
orders_90d | returns_90d | margin_90d(实际毛利) | loss_rate(退货损失率) | rate_source(SKU自己/运营×类目/运营全池/全店) |
operator(刘梦蝶/明瑞瑞/朱以超) | category | listed_days | cur_price | cost_price | assigned_at

## order_system.offerprice_listing —— 在卖offer快照（每晚同步）
platform('Lowes'/'Macy') | shop_name('autool'/'kuyotq') | shop_sku | warehouse_sku(供应商SKU) |
origin_price(原价) | discount_price(折扣价,即成交价) | price | active(1在卖) | category | listed_at | quantity

## order_system.lowes_order_data —— Lowes订单（每30分钟）
shop_id(10=autool) | order_id | offer_sku | created_date | order_state('CANCELED'要排除;'SHIPPING'待发;'WAITING_ACCEPTANCE'未接单) |
line_total_price(整单售价,不乘数量) 。macy_order_data/bestbuy_order_data 结构类似(shop_id见shop_configs)。

## order_system.return_case —— 退货台账（每天05:30重建）
store('Lowes-Autool'/'Macy-Kuyotq'…注意是这种写法) | shop_sku | order_id | order_date | return_date |
state('pending等退款/recovered已退/written_off核销/not_charged不算退货/warehouse退回海外仓') |
cost(成本货值) | sale(卖价) | exposure(敞口) | supplier('Costway'/'Vevor') | claim_filed(已登记) |
claim_tracking(退货跟踪号) | claim_result | recover_note 。算退货损失率时排除 state='not_charged'。

## order_system.profit_sku_90d —— SKU滚动90天利润
store | shop_sku | orders | sale | profit_gross(非退货毛利) | returns_cnt | loss_expected | net(净贡献) | margin(净利率)

## order_system.profit_month_cohort —— 月账本（月×运营×店铺）
order_month('2026-05') | operator | store | orders | sale | profit_gross | returns_cnt |
loss_expected(预计损失) | net / margin_net(预计口径) | loss_actual / net_actual / margin_net_actual(实际落袋口径) |
neg_profit / neg_n(亏本单)

## order_system.offer_price_change_log —— 改价/推送审计
run_id(前缀: mon-监控/plan-方案/batch-批推/manual-单推/full-导出/delist-删offer) | store_key | shop_sku |
status('dry_run候选/success成功/failed/skipped/alert') | triggered_at | old_origin_price | new_origin_price |
new_discount_price | profit_margin_before/after | decision_reason

## order_system.issue_log —— 问题清单
detected_date | issue_type('price_push_mismatch推价异常/delisted_but_selling下架仍出单/cell_below_baseline破线/negative_ev_sku负期望/recovery_overdue追款超期/return_spike退货异动') | entity('SKU@店') | severity | status('open/resolved')

## order_system.mirakl_returns —— 平台退货记录（每小时）
platform | shop_name | order_id | date_created | reason_code

## autooperate.newestdropship / newestdropship_vevor —— 供应商实时价
SKU(=warehouse_sku) | Price | Updated_At 。Lowes成本=Price×0.75。

## 常用口径提醒
- 运营按SKU前缀：ATCO-MDLW=刘梦蝶、ATCO-MRLW=明瑞瑞、ATCO-YCLW=朱以超
- 统计窗口(成熟口径)=created_date/order_date 在30~120天前
- 销售额直接SUM(line_total_price)，不乘数量
- 店铺名两套写法：store_key小写下划线(lowes_autool)、利润台账store带横杠(Lowes-Autool)
