"""数据库约束与受控执行器的回归测试（需要 MySQL 容器在跑）。

覆盖契约里被审查点名的三处：同 SKU 同周期只能有一条活跃建议、审计表只能追加、
执行幂等与"采购单已写入但建议未置 EXECUTED"的崩溃恢复。
"""

import pytest

from inv_agent import db, executor, guardrails, repository

TEST_SKU = "TEST-EXEC-001"


@pytest.fixture()
def test_sku(db_ready):
    with db.tx() as cur:
        cur.execute(
            "INSERT INTO supplier (supplier_code,name,lead_time_days,lead_time_sigma_days,"
            "payment_terms_days,credit_limit) VALUES ('TESTSUP','测试供应商',7,1.0,30,1000000) "
            "ON DUPLICATE KEY UPDATE name=VALUES(name)"
        )
        cur.execute("SELECT id FROM supplier WHERE supplier_code='TESTSUP'")
        supplier_id = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO sku (sku_code,name,category,supplier_id,unit_cost,price,lead_time_days,"
            "moq,pack_size,service_level) VALUES (%s,'测试商品','TEST',%s,100.00,199.00,7,1,1,0.95) "
            "ON DUPLICATE KEY UPDATE unit_cost=VALUES(unit_cost)",
            (TEST_SKU, supplier_id),
        )
        cur.execute("SELECT id FROM sku WHERE sku_code=%s", (TEST_SKU,))
        sku_id = cur.fetchone()["id"]
    yield {"sku_id": sku_id, "supplier_id": supplier_id}
    # 清理测试数据：必须把 sku/supplier 也删掉，否则残留会污染后面的批量扫描
    # （审计表按契约不可删除，测试痕迹会留在审计里）
    with db.tx() as cur:
        cur.execute("DELETE FROM inbound_order WHERE sku_id=%s", (sku_id,))
        cur.execute("DELETE FROM purchase_order WHERE sku_id=%s", (sku_id,))
        cur.execute(
            "DELETE FROM approval_record WHERE suggestion_id IN "
            "(SELECT id FROM replenishment_suggestion WHERE sku_id=%s)",
            (sku_id,),
        )
        cur.execute(
            "DELETE FROM replenishment_suggestion WHERE sku_id=%s", (sku_id,)
        )
        cur.execute("DELETE FROM exception_case WHERE sku_id=%s", (sku_id,))
        cur.execute("DELETE FROM policy_snapshot WHERE sku_id=%s", (sku_id,))
        cur.execute("DELETE FROM inventory_snapshot WHERE sku_id=%s", (sku_id,))
        cur.execute("DELETE FROM sales_daily WHERE sku_id=%s", (sku_id,))
        cur.execute("DELETE FROM sku WHERE id=%s", (sku_id,))
        cur.execute("DELETE FROM supplier WHERE id=%s", (supplier_id,))


def _make_suggestion(
    test_sku, period, qty=10, hash_seed="a", status="PENDING_APPROVAL"
):
    sku = repository.get_sku(test_sku["sku_id"])
    amount = round(qty * float(sku["unit_cost"]), 2)
    content_hash = (hash_seed * 64)[:64]
    sid = repository.insert_suggestion(
        sku_id=test_sku["sku_id"],
        period=period,
        qty=qty,
        amount=amount,
        unit_price=float(sku["unit_cost"]),
        basis=[{"source": "test"}],
        rule_trace=[{"rule": "test"}],
        content_hash=content_hash,
        status=status,
    )
    return sid, content_hash, amount


def test_only_one_active_suggestion_per_sku_period(test_sku, db_ready):
    period = "2099-W01"
    _make_suggestion(test_sku, period, hash_seed="a")
    with pytest.raises(Exception) as err:
        _make_suggestion(test_sku, period, hash_seed="b")
    assert "uk_sug_active" in str(err.value) or "1062" in str(err.value)


def test_same_content_is_idempotent(test_sku, db_ready):
    period = "2099-W02"
    first, _, _ = _make_suggestion(test_sku, period, hash_seed="c")
    second, _, _ = _make_suggestion(test_sku, period, hash_seed="c")
    assert first == second


def test_audit_event_is_append_only(db_ready):
    with db.tx() as cur:
        cur.execute(
            "INSERT INTO audit_event (entity,entity_id,event_type,actor,actor_role) "
            "VALUES ('test',1,'X','tester','ADMIN')"
        )
        audit_id = cur.lastrowid
    with pytest.raises(Exception):
        db.execute(
            "UPDATE audit_event SET event_type='Y' WHERE id=%s", (audit_id,)
        )
    with pytest.raises(Exception):
        db.execute("DELETE FROM audit_event WHERE id=%s", (audit_id,))


def test_execute_requires_approval(test_sku, db_ready):
    period = "2099-W03"
    sid, _, _ = _make_suggestion(test_sku, period, hash_seed="d")
    with pytest.raises(executor.ExecutionError) as err:
        executor.submit(sid, "bob", "SUPERVISOR")
    assert err.value.code == "APPROVAL_INVALIDATED"


def test_fault_after_po_insert_then_recover_keeps_single_po(
    test_sku, db_ready, monkeypatch
):
    """审查点名的最小验证：采购单已写入、建议未置 EXECUTED 时崩溃，恢复后不得新建第二张。"""
    period = "2099-W04"
    sid, content_hash, amount = _make_suggestion(
        test_sku, period, hash_seed="e"
    )
    sku = repository.get_sku(test_sku["sku_id"])
    supplier = repository.get_supplier(test_sku["supplier_id"])
    snapshot = guardrails.freeze_approval_snapshot(
        executor.params_from_config(),
        supplier,
        repository.get_suggestion(sid),
        sku_code=sku["sku_code"],
    )
    repository.record_approval(
        sid, "APPROVE", "bob", "SUPERVISOR", content_hash, snapshot
    )
    repository.set_status(sid, "PENDING_APPROVAL", "APPROVED", content_hash)
    monkeypatch.setenv("INV_FAULT_AFTER_PO_INSERT", "1")
    with pytest.raises(executor.InjectedFault):
        executor.submit(sid, "bob", "SUPERVISOR")
    monkeypatch.delenv("INV_FAULT_AFTER_PO_INSERT")
    # 崩溃现场：建议停在 EXECUTING，采购单已经存在
    assert repository.get_suggestion(sid)["status"] == "EXECUTING"
    key = executor.idempotency_key(sku["id"], period, content_hash)
    assert repository.find_po_by_idem(key) is not None
    result = executor.recover()
    assert result["recovered"] >= 1 and result["failed"] == 0
    assert repository.get_suggestion(sid)["status"] == "EXECUTED"
    rows = db.query_all(
        "SELECT id FROM purchase_order WHERE suggestion_id=%s", (sid,)
    )
    assert len(rows) == 1, "恢复后不得新建第二张采购单"


def test_reverse_po_uses_its_own_idempotency_key(test_sku, db_ready):
    period = "2099-W05"
    sid, content_hash, amount = _make_suggestion(
        test_sku, period, hash_seed="f"
    )
    sku = repository.get_sku(test_sku["sku_id"])
    supplier = repository.get_supplier(test_sku["supplier_id"])
    snapshot = guardrails.freeze_approval_snapshot(
        executor.params_from_config(),
        supplier,
        repository.get_suggestion(sid),
        sku_code=sku["sku_code"],
    )
    repository.record_approval(
        sid, "APPROVE", "bob", "SUPERVISOR", content_hash, snapshot
    )
    repository.set_status(sid, "PENDING_APPROVAL", "APPROVED", content_hash)
    executor.submit(sid, "bob", "SUPERVISOR")
    po = repository.db.query_one(
        "SELECT * FROM purchase_order WHERE suggestion_id=%s", (sid,)
    )
    first = executor.reverse_po(po["id"], "bob", "SUPERVISOR")
    second = executor.reverse_po(po["id"], "bob", "SUPERVISOR")
    assert first["replayed"] is False
    assert second["replayed"] is True, "反向单必须有自己的幂等键，不能重复生成"
