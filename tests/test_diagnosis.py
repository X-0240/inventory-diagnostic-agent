"""诊断 schema 单测：受限动作空间，枚举外与缺字段都必须被拒。"""

from inv_agent import diagnosis, llm


def valid_payload():
    return {
        "hypothesis_type": "PROMOTION_SURGE",
        "evidence_refs": [
            {
                "tool": "get_sales_series",
                "summary": "近 7 天日均翻倍",
                "timestamp": "2026-09-24T00:00:00Z",
            }
        ],
        "conflicting_evidence": False,
        "proposed_actions": [{"action": "ADVANCE", "rationale": "证据一致"}],
        "needs_human": False,
        "confidence": 0.7,
    }


def test_valid_payload_passes():
    assert diagnosis.validate_diagnosis(valid_payload())["ok"] is True


def test_rejects_hypothesis_outside_enum():
    payload = valid_payload()
    payload["hypothesis_type"] = "MADE_UP_CAUSE"
    result = diagnosis.validate_diagnosis(payload)
    assert result["ok"] is False
    assert any("假设类型" in e for e in result["errors"])


def test_rejects_action_outside_enum():
    payload = valid_payload()
    payload["proposed_actions"] = [{"action": "EXECUTE_NOW", "rationale": "x"}]
    assert diagnosis.validate_diagnosis(payload)["ok"] is False


def test_rejects_missing_fields_and_bad_confidence():
    payload = valid_payload()
    payload.pop("needs_human")
    payload["confidence"] = 1.5
    result = diagnosis.validate_diagnosis(payload)
    assert result["ok"] is False
    assert any("缺少字段" in e for e in result["errors"])
    assert any("confidence" in e for e in result["errors"])


def test_rejects_empty_evidence():
    payload = valid_payload()
    payload["evidence_refs"] = []
    assert diagnosis.validate_diagnosis(payload)["ok"] is False


def _base_signal():
    return {
        "sku_id": 1,
        "period": "2011-W20",
        "score": 4.0,
        "factors": {
            "demand": 0.0,
            "coverage": 0.0,
            "leadtime": 0.0,
            "supplier": 0.0,
            "flags": 0.0,
            "stockout": 0.0,
            "zeros": 0.0,
            "surge": 0.0,
            "shift": 0.0,
        },
        "baseline_daily": 5.0,
        "sigma": 2.0,
        "sample_days": 28,
        "days_with_sales": 20,
        "coverage_days": 9.0,
        "missing_days": 0,
        "evidence": [
            {"tool": "get_sales_series", "summary": "x", "timestamp": "t"}
        ],
    }


def test_stub_llm_output_passes_schema():
    import json

    out = llm.StubLLM().complete_json("s", json.dumps(_base_signal()))
    assert diagnosis.validate_diagnosis(out)["ok"] is True


def test_stub_hypothesis_follows_factor_priority():
    import json

    client = llm.StubLLM()
    signal = _base_signal()
    signal["days_with_sales"] = 3
    assert (
        client.complete_json("s", json.dumps(signal))["hypothesis_type"]
        == "NEW_PRODUCT_NO_HISTORY"
    )
    signal = _base_signal()
    signal["factors"]["surge"] = 0.5
    assert (
        client.complete_json("s", json.dumps(signal))["hypothesis_type"]
        == "PROMOTION_SURGE"
    )
    signal = _base_signal()
    signal["factors"]["shift"] = 0.4
    assert (
        client.complete_json("s", json.dumps(signal))["hypothesis_type"]
        == "DEMAND_SHIFT"
    )
    signal = _base_signal()
    signal["factors"]["leadtime"] = 1.5
    assert (
        client.complete_json("s", json.dumps(signal))["hypothesis_type"]
        == "SUPPLIER_DELAY"
    )
    signal = _base_signal()
    signal["factors"]["stockout"] = 2.0
    assert (
        client.complete_json("s", json.dumps(signal))["hypothesis_type"]
        == "STOCKOUT_CASCADE"
    )
    signal = _base_signal()
    signal["factors"]["zeros"] = 1.5
    assert (
        client.complete_json("s", json.dumps(signal))["hypothesis_type"]
        == "DATA_ANOMALY"
    )
