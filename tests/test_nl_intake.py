"""入口层测试：实体解析（含幻觉拒绝）、时间解析、冻结用例集、与图的衔接。"""
import pytest

from inv_agent import intake, repository
from inv_agent.nl_cases import CASES, resolve_marker

def test_resolve_sku_by_code(db_ready):
    r=intake.resolve_sku("23166")
    assert r["status"]=="OK" and r["sku"]["sku_code"]=="23166" and r["matched_by"]=="code"

def test_resolve_sku_by_unique_name(db_ready):
    r=intake.resolve_sku("商品23166")
    assert r["status"]=="OK" and r["sku"]["sku_code"]=="23166"

def test_resolve_sku_ambiguous_returns_candidates(db_ready):
    r=intake.resolve_sku("商品2")
    assert r["status"]=="AMBIGUOUS" and len(r["candidates"])>1

def test_resolve_sku_rejects_hallucinated_code(db_ready):
    r=intake.resolve_sku("SKU-999999")
    assert r["status"]=="NOT_FOUND"

def test_resolve_sku_empty_query(db_ready):
    assert intake.resolve_sku("")["status"]=="NOT_FOUND"

def test_resolve_period_explicit_and_relative(db_ready):
    latest=intake.latest_period()
    assert latest, "种子数据里应当有可用周期"
    assert intake.resolve_period("2011-W21")[0]=="2011-W21"
    assert intake.resolve_period("上周")[0]==intake.shift_period(latest,-1)
    assert intake.resolve_period("下周")[0]==intake.shift_period(latest,1)
    assert intake.resolve_period("")[0]==latest

@pytest.mark.parametrize("case",CASES,ids=[c["text"][:18] for c in CASES])
def test_frozen_nl_cases_with_stub(case,db_ready,monkeypatch):
    monkeypatch.setenv("LLM_FORCE_STUB","1")
    result=intake.parse(case["text"])
    expect=case["expect"]
    assert result["code"]==expect["code"], "用例「%s」期望 %s，实际 %s（%s）"%(
        case["text"],expect["code"],result["code"],result.get("message",""))
    if expect["code"]=="OK":
        latest=intake.latest_period()
        want_period=resolve_marker(expect["period"],latest,intake.shift_period)
        assert result["params"]["sku_code"]==expect["sku_code"]
        assert result["params"]["period"]==want_period, "周期解析不对：%s vs %s"%(
            result["params"]["period"],want_period)
        assert result["intent"]==expect["intent"]

def test_answer_wires_into_graph(db_ready,monkeypatch):
    """解析成功后应当能落到图上跑一次计划（同一条权威实现）。"""
    monkeypatch.setenv("LLM_FORCE_STUB","1")
    latest=intake.latest_period()
    period=intake.shift_period(latest,-9)   #找一个没跑过的周期，避免命中已有建议
    result=intake.answer("查一下 23166 在 %s 的库存"%period)
    assert result["ok"] is True, result
    assert result["params"]["period"]==period
    try:
        assert "suggestion_id" in result
    finally:
        if result.get("suggestion_id"):
            with repository.db.tx() as cur:
                cur.execute("DELETE FROM replenishment_suggestion WHERE id=%s",(result["suggestion_id"],))
                cur.execute("DELETE FROM exception_case WHERE sku_id=%s AND period=%s",
                            (result["sku"]["id"],period))
                cur.execute("DELETE FROM policy_snapshot WHERE sku_id=%s AND period=%s",
                            (result["sku"]["id"],period))
