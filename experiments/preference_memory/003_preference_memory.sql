-- 契约 v3.1 变更：新增第 14 张表 preference_memory（长期记忆的状态载体）
-- 理由：审计表只能记「发生过什么」，表达不了「这条偏好现在还有效吗」；
--       记忆必须有当前状态（ACTIVE/EXPIRED/SUPERSEDED）与过期时间，否则无法做遗忘。
-- 影响面：只新增一张表，不改动既有 13 张表的结构与语义；recall 只读，remember/forget/sweep 只写本表 + 审计。

CREATE TABLE IF NOT EXISTS preference_memory (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  scope ENUM('GLOBAL','SUPPLIER','SKU') NOT NULL,
  scope_key VARCHAR(32) NOT NULL,                 -- SKU 用 sku_code，SUPPLIER 用 supplier_code，GLOBAL 用 '-'
  kind ENUM('PREFER_ACTION','AVOID_ACTION','REQUIRE_HUMAN','LEADTIME_NOTE','SERVICE_LEVEL_NOTE') NOT NULL,
  value_json JSON NOT NULL,
  note VARCHAR(255) DEFAULT NULL,
  confidence DECIMAL(6,3) NOT NULL DEFAULT 1.000,
  source ENUM('HUMAN','INFERRED') NOT NULL DEFAULT 'HUMAN',
  status ENUM('ACTIVE','EXPIRED','SUPERSEDED') NOT NULL DEFAULT 'ACTIVE',
  superseded_by BIGINT UNSIGNED DEFAULT NULL,
  created_by VARCHAR(64) NOT NULL,
  expires_at DATETIME DEFAULT NULL,               -- NULL 表示不过期
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  active_flag TINYINT GENERATED ALWAYS AS (IF(status='ACTIVE',1,NULL)) STORED,
  PRIMARY KEY (id),
  UNIQUE KEY uk_memory_active (scope, scope_key, kind, active_flag),
  KEY idx_memory_lookup (scope, scope_key, kind, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
