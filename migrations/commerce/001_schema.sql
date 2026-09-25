-- 载体库 commerce：模拟电商/零售平台侧的商品、库存、在途、销量与收货流水
-- 与 Agent 库 inventory 分离；账号也分离（commerce_svc vs inv_app）
-- 部署方式：scripts/migrate_commerce.py 用 root 执行本文件

CREATE DATABASE IF NOT EXISTS commerce
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;

USE commerce;

-- 商品与参数：交期、账期、起订量、包装倍数、服务水平都在这里
CREATE TABLE IF NOT EXISTS product (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  sku_code VARCHAR(32) NOT NULL,
  name VARCHAR(128) NOT NULL,
  category VARCHAR(64) DEFAULT NULL,
  supplier_code VARCHAR(32) NOT NULL,
  supplier_name VARCHAR(128) NOT NULL,
  unit_cost DECIMAL(14,2) NOT NULL,
  price DECIMAL(14,2) NOT NULL,
  lead_time_days INT NOT NULL,
  lead_time_sigma_days DECIMAL(6,2) NOT NULL DEFAULT 0,
  payment_terms_days INT NOT NULL DEFAULT 0,
  credit_limit DECIMAL(14,2) NOT NULL DEFAULT 0,
  moq INT NOT NULL DEFAULT 1,
  pack_size INT NOT NULL DEFAULT 1,
  service_level DECIMAL(6,3) NOT NULL DEFAULT 0.950,
  status ENUM('ACTIVE','INACTIVE') NOT NULL DEFAULT 'ACTIVE',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_product_sku (sku_code),
  KEY idx_product_supplier (supplier_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 库存快照：口径与 Agent 库一致（可用＝在手-预留，不含在途）
CREATE TABLE IF NOT EXISTS inventory_snapshot (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  sku_code VARCHAR(32) NOT NULL,
  warehouse_id VARCHAR(32) NOT NULL,
  snapshot_date DATE NOT NULL,
  qty_on_hand INT NOT NULL,
  qty_reserved INT NOT NULL DEFAULT 0,
  version INT NOT NULL DEFAULT 1,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_ci_sku_wh_date (sku_code, warehouse_id, snapshot_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 在途：Agent 判断「交期内能不能到」的唯一依据
CREATE TABLE IF NOT EXISTS inbound (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  sku_code VARCHAR(32) NOT NULL,
  warehouse_id VARCHAR(32) NOT NULL DEFAULT 'WH1',
  po_no VARCHAR(48) NOT NULL,
  qty INT NOT NULL,
  expected_date DATE NOT NULL,
  actual_date DATE DEFAULT NULL,
  status ENUM('IN_TRANSIT','RECEIVED','CANCELLED') NOT NULL DEFAULT 'IN_TRANSIT',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_inbound_sku_status (sku_code, status, expected_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 销量日粒度：Agent 的基线需求估算只拿它当需求信号
CREATE TABLE IF NOT EXISTS sales_daily (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  sku_code VARCHAR(32) NOT NULL,
  sale_date DATE NOT NULL,
  qty INT NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_sales_sku_date (sku_code, sale_date),
  KEY idx_sales_date (sale_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 收货确认流水：载体自己的写操作，幂等键唯一
CREATE TABLE IF NOT EXISTS receipt (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  sku_code VARCHAR(32) NOT NULL,
  po_no VARCHAR(48) NOT NULL,
  qty INT NOT NULL,
  occurred_at DATETIME NOT NULL,
  idempotency_key VARCHAR(96) NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_receipt_idem (idempotency_key),
  KEY idx_receipt_sku (sku_code, po_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 载体服务账号：只对 commerce 库有权限；Agent 账号 inv_app 对它没有任何权限
CREATE USER IF NOT EXISTS 'commerce_svc'@'%' IDENTIFIED BY 'commerce_local_svc';
GRANT SELECT,INSERT,UPDATE,DELETE ON commerce.* TO 'commerce_svc'@'%';
FLUSH PRIVILEGES;
