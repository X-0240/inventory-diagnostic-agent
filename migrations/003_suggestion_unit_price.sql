-- v1.6：建议表补 unit_price（单价绑定金额与内容哈希，执行前复检）
-- 001 里已给新库加过这一列，这里只给已存在的库补，幂等

SET @col_exists := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='replenishment_suggestion'
    AND COLUMN_NAME='unit_price');
SET @ddl := IF(@col_exists=0,
  'ALTER TABLE replenishment_suggestion ADD COLUMN unit_price DECIMAL(14,2) NULL AFTER amount',
  'SELECT 1');
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
