-- v1.7：载体事实加版本字段，让 Agent 能判断"拿到的是不是最新的"
-- product/inbound 加 data_version，inventory_snapshot/inbound 加 updated_at
-- 已建库要补列，所以用 information_schema 判空后再 ALTER（MySQL 8 没有 ADD COLUMN IF NOT EXISTS）

USE commerce;

SET @exists := (SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='product' AND COLUMN_NAME='data_version');
SET @ddl := IF(@exists=0,
  'ALTER TABLE product ADD COLUMN data_version BIGINT UNSIGNED NOT NULL DEFAULT 1 AFTER status',
  'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @exists := (SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='inbound' AND COLUMN_NAME='data_version');
SET @ddl := IF(@exists=0,
  'ALTER TABLE inbound ADD COLUMN data_version BIGINT UNSIGNED NOT NULL DEFAULT 1 AFTER status',
  'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @exists := (SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='inbound' AND COLUMN_NAME='updated_at');
SET @ddl := IF(@exists=0,
  'ALTER TABLE inbound ADD COLUMN updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP',
  'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @exists := (SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='inventory_snapshot' AND COLUMN_NAME='updated_at');
SET @ddl := IF(@exists=0,
  'ALTER TABLE inventory_snapshot ADD COLUMN updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP',
  'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 批量取数靠这两条索引：一次按 SKU 列表取，避免接口内部退化成 N+1
SET @exists := (SELECT COUNT(*) FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='sales_daily' AND INDEX_NAME='idx_sales_sku_date');
SET @ddl := IF(@exists=0,'ALTER TABLE sales_daily ADD KEY idx_sales_sku_date (sku_code,sale_date)','SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @exists := (SELECT COUNT(*) FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='inventory_snapshot' AND INDEX_NAME='idx_ci_sku_date');
SET @ddl := IF(@exists=0,'ALTER TABLE inventory_snapshot ADD KEY idx_ci_sku_date (sku_code,snapshot_date)','SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
