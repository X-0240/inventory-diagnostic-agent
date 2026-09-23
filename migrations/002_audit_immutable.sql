-- 契约 10.1：audit_event 不可变，只允许 INSERT
-- 库层用触发器强制，避免应用层写错就改掉证据

DROP TRIGGER IF EXISTS trg_audit_no_update;
CREATE TRIGGER trg_audit_no_update BEFORE UPDATE ON audit_event
FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit_event is append-only';

DROP TRIGGER IF EXISTS trg_audit_no_delete;
CREATE TRIGGER trg_audit_no_delete BEFORE DELETE ON audit_event
FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit_event is append-only';
