-- =====================================================================
-- Lowe's Autool 店铺接入 SQL
-- 执行顺序：先建表 → 插 shop_configs → 验证
-- 代理 IP / User-Agent / 端口 / 账号密码 用你自己的值替换
-- =====================================================================

-- 1) 订单表（Excel 导入 + 手动发货用）
CREATE TABLE IF NOT EXISTS `autooperate`.`lowesorder` LIKE `autooperate`.`macyorder`;

-- 2) Mirakl API 拉取的订单数据（订单同步用）
CREATE TABLE IF NOT EXISTS `order_system`.`lowes_order_data` LIKE `order_system`.`macy_order_data`;

-- 3) 交易流水表
CREATE TABLE IF NOT EXISTS `order_system`.`lowes_autool_transaction_logs` LIKE `order_system`.`macy_kuyotq_transaction_logs`;

-- 4) 插入店铺配置（代理/UA 替换成真实值）
INSERT INTO `order_system`.`shop_configs`
  (platform, shop_name, proxy_ip, proxy_port, proxy_user, proxy_pass, user_agent, is_active)
VALUES
  ('lowes-autool', 'autool',
   '<proxy_ip>',      -- 例：161.77.xxx.xxx
   '<proxy_port>',    -- 例：50100
   '<proxy_user>',
   '<proxy_pass>',
   '<user_agent>',    -- 紫鸟给的那串 UA
   1);

-- 5) 验证
SELECT id, platform, shop_name, proxy_ip, proxy_port, is_active
FROM `order_system`.`shop_configs`
WHERE platform = 'lowes-autool' AND shop_name = 'autool';
