-- =====================================================================
-- Lowe's Yasonic 店铺接入 SQL
-- 执行顺序：先建表 → 插 shop_configs → 验证 → 记下分配到的 shop_configs.id
-- =====================================================================

-- 1) 订单表 lowesorder 已存在（autool 共用），不需要建
-- 2) lowes_order_data 已存在（autool 共用），不需要建

-- 3) 交易流水表（每店一张）
CREATE TABLE IF NOT EXISTS `order_system`.`lowes_yasonic_transaction_logs`
  LIKE `order_system`.`lowes_autool_transaction_logs`;

-- 4) 插入店铺配置
INSERT INTO `order_system`.`shop_configs`
  (platform, shop_name, proxy_ip, proxy_port, proxy_user, proxy_pass, user_agent, is_active)
VALUES
  ('lowes-yasonic', 'yasonic',
   '208.214.165.240',
   '50100',
   'mrrssgm22',
   'YDrXX3QIdX',
   'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0',
   1);

-- 5) 验证 + 记录分配到的 id（后面 db_manager.py 的 CASE WHEN 要用）
SELECT id, platform, shop_name, proxy_ip, is_active
FROM `order_system`.`shop_configs`
WHERE platform = 'lowes-yasonic' AND shop_name = 'yasonic';

-- 6) 验证交易日志表结构（应该和 lowes_autool 一致）
SELECT COUNT(*) AS col_cnt
FROM information_schema.columns
WHERE table_schema='order_system' AND table_name='lowes_yasonic_transaction_logs';
