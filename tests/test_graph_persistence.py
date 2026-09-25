"""走图落库的回归测试。

为什么单独测这条：场景测试是直接调 pipeline 与桩的，绕过了 LangGraph，
所以"归因结论没写进建议单"这种状态通道问题它抓不到（曾经真的漏了：RunState 少声明 diagnosis）。
"""
import pytest

from inv_agent import graph, pipeline, repository

TEST_PERIOD="2011-W25"

def test_diagnosis_reaches_suggestion(db_ready,monkeypatch):
    #强制走桩：回归测试不该花钱也不该等真实模型
    monkeypatch.setenv("LLM_FORCE_STUB","1")
    picked=None
    for sku in repository.active_skus(limit=40):
        try:
            plan=pipeline.evaluate(sku,TEST_PERIOD)
        except pipeline.PlanError:
            continue
        if plan["qty"]<=0:
            continue
        picked=sku
        result=graph.run(sku["sku_code"],TEST_PERIOD,mode="plan",diagnose=True)
        sid=result.get("suggestion_id")
        assert sid, "应当落库一条建议"
        row=repository.get_suggestion(sid)
        assert row["hypothesis_type"] is not None, "归因假设必须随建议单一起落库"
        assert row["confidence"] is not None, "置信度必须落库"
        assert row["proposed_actions"] is not None, "给审批人看的建议清单必须落库"
        assert row["case_id"] is not None, "应当关联异常案件"
        #清理本次产生的业务数据（审计按契约不可删）
        with repository.db.tx() as cur:
            cur.execute("DELETE FROM replenishment_suggestion WHERE id=%s",(sid,))
            cur.execute("DELETE FROM exception_case WHERE sku_id=%s AND period=%s",
                        (sku["id"],TEST_PERIOD))
            cur.execute("DELETE FROM policy_snapshot WHERE sku_id=%s AND period=%s",
                        (sku["id"],TEST_PERIOD))
        break
    if not picked:
        pytest.skip("种子数据里没找到能产生建议的 SKU")

def test_plan_skip_does_not_create_suggestion(db_ready,monkeypatch):
    """不需要补货的 SKU 不应产生建议单（早期版本会给 qty=0 也建一条）。"""
    monkeypatch.setenv("LLM_FORCE_STUB","1")
    for sku in repository.active_skus(limit=40):
        try:
            plan=pipeline.evaluate(sku,TEST_PERIOD)
        except pipeline.PlanError:
            continue
        if plan["qty"]!=0:
            continue
        result=graph.run(sku["sku_code"],TEST_PERIOD,mode="plan",diagnose=False)
        assert not result.get("suggestion_id"), "qty=0 不应落建议单"
        return
    pytest.skip("种子数据里没找到无需补货的 SKU")
