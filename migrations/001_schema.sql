-- 契约第 10.1 节的 13 张表；本文件是权威 schema，改动必须升版本号
-- 约定：InnoDB + utf8mb4；主键 BIGINT UNSIGNED；金额 DECIMAL(14,2)；数量 INT；比率 DECIMAL(6,3)
-- 时间列 DATETIME，库内统一 UTC（容器 default-time-zone=+00:00）

SET NAMES utf8mb4;

-- 供应商
CREATE TABLE IF NOT EXISTS supplier (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  supplier_code VARCHAR(32) NOT NULL,
  name VARCHAR(128) NOT NULL,
  lead_time_days INT NOT NULL,
  lead_time_sigma_days DECIMAL(6,2) NOT NULL DEFAULT 0,
  payment_terms_days INT NOT NULL,
  credit_limit DECIMAL(14,2) NOT NULL DEFAULT 0,
  status ENUM('ACTIVE','INACTIVE') NOT NULL DEFAULT 'ACTIVE',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_supplier_code (supplier_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- SKU
CREATE TABLE IF NOT EXISTS sku (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  sku_code VARCHAR(32) NOT NULL,
  name VARCHAR(128) NOT NULL,
  category VARCHAR(64) DEFAULT NULL,
  supplier_id BIGINT UNSIGNED NOT NULL,
  unit_cost DECIMAL(14,2) NOT NULL,
  price DECIMAL(14,2) NOT NULL,
  lead_time_days INT NOT NULL,
  moq INT NOT NULL DEFAULT 1,
  pack_size INT NOT NULL DEFAULT 1,
  service_level DECIMAL(6,3) NOT NULL DEFAULT 0.950,
  status ENUM('ACTIVE','INACTIVE') NOT NULL DEFAULT 'ACTIVE',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_sku_code (sku_code),
  KEY idx_sku_supplier_status (supplier_id, status),
  CONSTRAINT fk_sku_supplier FOREIGN KEY (supplier_id) REFERENCES supplier (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 库存快照（可用库存口径：qty_on_hand - qty_reserved，不含在途）
CREATE TABLE IF NOT EXISTS inventory_snapshot (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  sku_id BIGINT UNSIGNED NOT NULL,
  warehouse_id VARCHAR(32) NOT NULL,
  snapshot_date DATE NOT NULL,
  qty_on_hand INT NOT NULL,
  qty_reserved INT NOT NULL DEFAULT 0,
  qty_inbound INT NOT NULL DEFAULT 0,
  version INT NOT NULL DEFAULT 1,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_inv_sku_wh_date (sku_id, warehouse_id, snapshot_date),
  CONSTRAINT fk_inv_sku FOREIGN KEY (sku_id) REFERENCES sku (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 销量（sale_date 为发货日；source_flag 标注来源：公开数据 or 模拟）
CREATE TABLE IF NOT EXISTS sales_daily (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  sku_id BIGINT UNSIGNED NOT NULL,
  sale_date DATE NOT NULL,
  qty INT NOT NULL,
  source_flag ENUM('PUBLIC','SIMULATED') NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_sales_sku_date (sku_id, sale_date),
  CONSTRAINT fk_sales_sku FOREIGN KEY (sku_id) REFERENCES sku (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 在途（权威来源；inventory_snapshot.qty_inbound 只是冗余快照）
CREATE TABLE IF NOT EXISTS purchase_order (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  po_no VARCHAR(32) NOT NULL,
  suggestion_id BIGINT UNSIGNED DEFAULT NULL,
  supplier_id BIGINT UNSIGNED NOT NULL,
  sku_id BIGINT UNSIGNED NOT NULL,
  qty INT NOT NULL,
  unit_price DECIMAL(14,2) NOT NULL,
  total_amount DECIMAL(14,2) NOT NULL,
  status ENUM('DRAFT','SUBMITTED','CONFIRMED','RECEIVED','CLOSED','CANCELLED') NOT NULL DEFAULT 'DRAFT',
  idempotency_key VARCHAR(96) NOT NULL,
  approval_snapshot_json JSON DEFAULT NULL,
  reverse_of_po_id BIGINT UNSIGNED DEFAULT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  submitted_at DATETIME DEFAULT NULL,
  confirmed_at DATETIME DEFAULT NULL,
  received_at DATETIME DEFAULT NULL,
  closed_at DATETIME DEFAULT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_po_no (po_no),
  UNIQUE KEY uk_po_idem (idempotency_key),
  KEY idx_po_supplier_status (supplier_id, status),
  KEY idx_po_sku_status (sku_id, status),
  CONSTRAINT fk_po_supplier FOREIGN KEY (supplier_id) REFERENCES supplier (id),
  CONSTRAINT fk_po_sku FOREIGN KEY (sku_id) REFERENCES sku (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS inbound_order (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  sku_id BIGINT UNSIGNED NOT NULL,
  po_id BIGINT UNSIGNED DEFAULT NULL,
  qty INT NOT NULL,
  expected_date DATE NOT NULL,
  actual_date DATE DEFAULT NULL,
  status ENUM('IN_TRANSIT','RECEIVED','CANCELLED') NOT NULL DEFAULT 'IN_TRANSIT',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_inbound_sku_status_date (sku_id, status, expected_date),
  CONSTRAINT fk_inbound_sku FOREIGN KEY (sku_id) REFERENCES sku (id),
  CONSTRAINT fk_inbound_po FOREIGN KEY (po_id) REFERENCES purchase_order (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 策略与基线快照（每次重算一行，rule_version 用于冻结与追溯）
CREATE TABLE IF NOT EXISTS policy_snapshot (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  sku_id BIGINT UNSIGNED NOT NULL,
  period VARCHAR(10) NOT NULL,
  method VARCHAR(32) NOT NULL,
  baseline_daily DECIMAL(14,3) NOT NULL,
  sigma DECIMAL(14,3) NOT NULL,
  sample_days INT NOT NULL,
  service_level DECIMAL(6,3) NOT NULL,
  z DECIMAL(6,3) NOT NULL,
  safety_qty INT NOT NULL,
  reorder_point INT NOT NULL,
  target_qty INT NOT NULL,
  rule_version VARCHAR(16) NOT NULL,
  computed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_policy_sku_period_rule (sku_id, period, rule_version),
  CONSTRAINT fk_policy_sku FOREIGN KEY (sku_id) REFERENCES sku (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 异常案件（不存 suggestion_id：一对多由 replenishment_suggestion.case_id 表达）
CREATE TABLE IF NOT EXISTS exception_case (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  sku_id BIGINT UNSIGNED NOT NULL,
  period VARCHAR(10) NOT NULL,
  status ENUM('OPEN','INVESTIGATING','DECIDED','CLOSED','MANUAL_TAKEOVER') NOT NULL DEFAULT 'OPEN',
  hypothesis_type ENUM('PROMOTION_SURGE','NEW_PRODUCT_NO_HISTORY','SUPPLIER_DELAY','STOCKOUT_CASCADE','DEMAND_SHIFT','DATA_ANOMALY') DEFAULT NULL,
  confidence DECIMAL(6,3) DEFAULT NULL,
  owner VARCHAR(64) DEFAULT NULL,
  version INT NOT NULL DEFAULT 1,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_case_sku_period (sku_id, period),
  CONSTRAINT fk_case_sku FOREIGN KEY (sku_id) REFERENCES sku (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 补货建议（唯一活跃建议由 active_flag 生成列 + 唯一索引在库层强制）
CREATE TABLE IF NOT EXISTS replenishment_suggestion (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  sku_id BIGINT UNSIGNED NOT NULL,
  period VARCHAR(10) NOT NULL,
  case_id BIGINT UNSIGNED DEFAULT NULL,
  qty INT NOT NULL,
  amount DECIMAL(14,2) NOT NULL,
  basis_json JSON NOT NULL,
  rule_trace_json JSON NOT NULL,
  hypothesis_type ENUM('PROMOTION_SURGE','NEW_PRODUCT_NO_HISTORY','SUPPLIER_DELAY','STOCKOUT_CASCADE','DEMAND_SHIFT','DATA_ANOMALY') DEFAULT NULL,
  evidence_refs JSON DEFAULT NULL,
  conflicting_evidence TINYINT(1) NOT NULL DEFAULT 0,
  proposed_actions JSON DEFAULT NULL,
  confidence DECIMAL(6,3) DEFAULT NULL,
  content_hash CHAR(64) NOT NULL,
  status ENUM('DRAFT','PENDING_APPROVAL','APPROVED','EXECUTING','EXECUTED','CLOSED','REJECTED','EXECUTION_FAILED','MANUAL_TAKEOVER','SUPERSEDED') NOT NULL DEFAULT 'DRAFT',
  version INT NOT NULL DEFAULT 1,
  immutable_after_approve TINYINT(1) NOT NULL DEFAULT 1,
  active_flag TINYINT GENERATED ALWAYS AS (IF(status IN ('DRAFT','PENDING_APPROVAL','APPROVED','EXECUTING'),1,NULL)) STORED,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_sug_sku_period_hash (sku_id, period, content_hash),
  UNIQUE KEY uk_sug_active (sku_id, period, active_flag),
  KEY idx_sug_status_updated (status, updated_at),
  CONSTRAINT fk_sug_sku FOREIGN KEY (sku_id) REFERENCES sku (id),
  CONSTRAINT fk_sug_case FOREIGN KEY (case_id) REFERENCES exception_case (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 审批记录（同一版本只能审批一次）
CREATE TABLE IF NOT EXISTS approval_record (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  suggestion_id BIGINT UNSIGNED NOT NULL,
  decision ENUM('APPROVE','REJECT','MODIFY') NOT NULL,
  decided_by VARCHAR(64) NOT NULL,
  decided_role ENUM('BUYER','APPROVER','SUPERVISOR','ADMIN') NOT NULL,
  content_hash CHAR(64) NOT NULL,
  approval_snapshot_json JSON NOT NULL,
  comment VARCHAR(255) DEFAULT NULL,
  decided_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_appr_sug_hash (suggestion_id, content_hash),
  CONSTRAINT fk_appr_sug FOREIGN KEY (suggestion_id) REFERENCES replenishment_suggestion (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 审计事件（不可变：库层用触发器禁止 UPDATE/DELETE）
CREATE TABLE IF NOT EXISTS audit_event (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  entity VARCHAR(32) NOT NULL,
  entity_id BIGINT UNSIGNED NOT NULL,
  event_type VARCHAR(48) NOT NULL,
  actor VARCHAR(64) NOT NULL,
  actor_role VARCHAR(16) NOT NULL,
  payload_json JSON DEFAULT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_audit_entity (entity, entity_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 任务运行（UNIQUE(job_name, period) 兼作 job_lock）
CREATE TABLE IF NOT EXISTS job_run (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  job_name VARCHAR(48) NOT NULL,
  period VARCHAR(10) NOT NULL,
  status ENUM('RUNNING','SUCCEEDED','FAILED','DEAD_LETTER') NOT NULL DEFAULT 'RUNNING',
  retry_count INT NOT NULL DEFAULT 0,
  next_retry_at DATETIME DEFAULT NULL,
  last_error VARCHAR(255) DEFAULT NULL,
  error_code VARCHAR(32) DEFAULT NULL,
  dead_letter TINYINT(1) NOT NULL DEFAULT 0,
  side_effect_status ENUM('NONE','POSSIBLE','CONFIRMED') NOT NULL DEFAULT 'NONE',
  reconcile_action VARCHAR(64) DEFAULT NULL,
  started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at DATETIME DEFAULT NULL,
  stats_json JSON DEFAULT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_job_period (job_name, period),
  KEY idx_job_status_retry (status, next_retry_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 配置版本（参数冻结与预注册的凭证）
CREATE TABLE IF NOT EXISTS config_version (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  config_key VARCHAR(48) NOT NULL,
  rule_version VARCHAR(16) NOT NULL,
  params_json JSON NOT NULL,
  frozen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  frozen_by VARCHAR(64) NOT NULL,
  note VARCHAR(255) DEFAULT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_config_key_version (config_key, rule_version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
