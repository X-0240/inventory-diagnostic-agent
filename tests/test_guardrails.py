"""护栏单测：金额、库存上限、供应商额度、账期，以及审批快照复检。"""

from inv_agent import guardrails

PARAMS = {
    "rule_version": "v1",
    "inventory_cap_multiplier": 2.0,
    "approver_amount_limit": 5000,
}


def supplier(**kw):
    base = {"payment_terms_days": 30, "credit_limit": 100000}
    base.update(kw)
    return base


def context(**kw):
    base = {
        "amount": 1000,
        "available": 10,
        "inbound_due": 0,
        "qty": 100,
        "target_qty": 200,
        "supplier": supplier(),
        "open_po_amount": 0,
        "role_amount_limit": None,
    }
    base.update(kw)
    return base


def test_validate_ok_path():
    result = guardrails.validate(context(), PARAMS)
    assert result["ok"] is True and result["violations"] == []


def test_inventory_cap_violation():
    result = guardrails.validate(context(qty=1000, target_qty=200), PARAMS)
    assert result["ok"] is False
    assert result["violations"][0]["code"] == "INVENTORY_OVER_CAP"


def test_supplier_credit_limit_violation():
    result = guardrails.validate(
        context(
            open_po_amount=99000,
            amount=5000,
            supplier=supplier(credit_limit=100000),
        ),
        PARAMS,
    )
    assert result["ok"] is False
    assert any(
        v["code"] == "SUPPLIER_CREDIT_EXCEEDED" for v in result["violations"]
    )


def test_payment_terms_missing_blocks_flow():
    result = guardrails.validate(
        context(supplier=supplier(payment_terms_days=None)), PARAMS
    )
    assert result["ok"] is False
    assert any(
        v["code"] == "PAYMENT_TERMS_INVALID" for v in result["violations"]
    )


def test_amount_role_limit_is_reported_when_given():
    result = guardrails.validate(
        context(amount=9000, role_amount_limit=5000), PARAMS
    )
    assert any(
        v["code"] == "AMOUNT_OVER_ROLE_LIMIT" for v in result["violations"]
    )


def test_freeze_and_reverify_detects_param_change():
    snap = guardrails.freeze_snapshot(PARAMS, supplier())
    assert guardrails.reverify(snap, PARAMS, supplier()) == []
    changed = dict(PARAMS, inventory_cap_multiplier=3.0)
    assert (
        guardrails.reverify(snap, changed, supplier())[0]["code"]
        == "INVENTORY_CAP_CHANGED"
    )
    assert (
        guardrails.reverify(snap, PARAMS, supplier(payment_terms_days=60))[0][
            "code"
        ]
        == "PAYMENT_TERMS_CHANGED"
    )
