"""场景集测试：只断言两件能站住的事——设计信号在数据里可见、诊断输出始终合法。

不做的事（重要）：不把"桩的假设分类命中率"当准确率。当前没有 LLM key，诊断走确定性桩，
桩的判据是我写的启发式；拿它去拟合我自己设计的场景，等于自出题自评分。
所以分类一致性只打印出来供人看，不作断言。
"""
import json

import pytest

from inv_agent import config, diagnosis, llm, pipeline, repository
from inv_agent.scenarios import (SCENARIO_SPEC, load_scenarios, split_for_scenario)

def _scenarios():
    mapping=load_scenarios()
    if not mapping:
        pytest.skip("缺少 data/scenarios.json，先跑 python -m inv_agent.data_pipeline --build")
    return mapping

def _samples(scenario,period,limit=6):
    scenarios=_scenarios()
    codes=[c for c,s in scenarios.items() if s==scenario]
    dev,held_out=split_for_scenario(scenario,codes)
    out=[]
    for label,group in (("dev",dev),("held_out",held_out)):
        for code in group[:limit]:
            sku=repository.db.query_one("SELECT * FROM sku WHERE sku_code=%s",(code,))
            if not sku:
                continue
            try:
                plan=pipeline.evaluate(sku,period)
            except pipeline.PlanError as e:
                out.append({"sku":code,"split":label,"error":e.code})
                continue
            facts=plan["facts"]
            signal=diagnosis.build_signal(facts,plan["score"],plan["score_detail"],plan["evidence"])
            hypothesis=llm.StubLLM().complete_json("s",json.dumps(signal,ensure_ascii=False))["hypothesis_type"]
            out.append({"sku":code,"split":label,"score":plan["score"],"qty":plan["qty"],
                        "surge_ratio":plan["surge_ratio"],"shift_ratio":plan["shift_ratio"],
                        "leadtime_bias_days":facts["leadtime_bias_days"],
                        "coverage_days":plan["coverage_days"],
                        "days_with_sales":facts["days_with_sales"],
                        "sale_days_56":len([q for q in facts["qty_series"] if q>0]),
                        "zero_days_7":facts["zero_days_7"],
                        "hypothesis":hypothesis})
    return out

def _signal_visible(sample,scenario):
    """返回值：True/False 表示信号可见与否；None 表示样本不足以判断（不计入分母）。"""
    if "error" in sample:
        return None
    #8 周内不足 8 天有销量的 SKU，水平类判断不可靠，单独排除；
    #但"历史很短"本身就是 NEW_PRODUCT 场景的信号，这类场景不排除
    if scenario!="NEW_PRODUCT_NO_HISTORY" and sample.get("sale_days_56",0)<8:
        return None
    if scenario=="PROMOTION_SURGE":
        return sample["surge_ratio"]>=1.2
    if scenario=="SUPPLIER_DELAY":
        return sample["leadtime_bias_days"]>=7
    if scenario=="NEW_PRODUCT_NO_HISTORY":
        return sample["days_with_sales"]<7
    if scenario=="STOCKOUT_CASCADE":
        return sample["coverage_days"]<=3
    if scenario=="DEMAND_SHIFT":
        return sample["shift_ratio"]>=1.2
    if scenario=="DATA_ANOMALY":
        return sample["zero_days_7"]>=2
    return False

@pytest.mark.parametrize("scenario",sorted(SCENARIO_SPEC.keys()))
def test_designed_signal_is_visible_in_data(scenario,db_ready):
    spec=SCENARIO_SPEC[scenario]
    samples=_samples(scenario,spec["period"])
    judged=[s for s in samples if _signal_visible(s,scenario) is not None]
    ok=[s for s in judged if _signal_visible(s,scenario)]
    excluded=len(samples)-len(judged)
    ratio=len(ok)/len(judged) if judged else 0
    print("\n["+scenario+"] 周期 "+spec["period"]+" 设计信号可见 %d/%d（判据 %s，样本不足排除 %d）"%(
        len(ok),len(judged),spec["signal"],excluded))
    assert ratio>=spec["min_ratio"], (
        "场景 %s 的设计信号在数据里不够明显：判据 %s，命中 %d/%d（排除 %d）"%(
            scenario,spec["signal"],len(ok),len(judged),excluded))

@pytest.mark.parametrize("scenario",sorted(SCENARIO_SPEC.keys()))
def test_diagnosis_output_is_always_schema_valid(scenario,db_ready):
    samples=_samples(scenario,SCENARIO_SPEC[scenario]["period"])
    hypotheses={}
    for s in samples:
        if "error" in s:
            continue
        hypotheses[s["hypothesis"]]=hypotheses.get(s["hypothesis"],0)+1
        assert s["hypothesis"] in diagnosis.HYPOTHESES
    print("\n["+scenario+"] 桩输出的假设分布（仅报告，不作准确率结论）: "+
          json.dumps(hypotheses,ensure_ascii=False))
    assert hypotheses, "该场景没有可用样本"

def test_normal_and_stressed_skus_are_separable(db_ready):
    """分界有区分度：压力场景的平均异常分应高于正常 SKU。"""
    scenarios=_scenarios()
    period="2011-W30"
    normal=[c for c,s in scenarios.items() if s=="NORMAL"][:10]
    stressed=[c for c,s in scenarios.items() if s in ("STOCKOUT_CASCADE","SUPPLIER_DELAY")][:10]
    def scores(codes):
        out=[]
        for code in codes:
            sku=repository.db.query_one("SELECT * FROM sku WHERE sku_code=%s",(code,))
            try:
                out.append(pipeline.evaluate(sku,period)["score"])
            except pipeline.PlanError:
                continue
        return out
    normal_scores=scores(normal)
    stressed_scores=scores(stressed)
    if not normal_scores or not stressed_scores:
        pytest.skip("样本不足")
    avg_normal=sum(normal_scores)/len(normal_scores)
    avg_stressed=sum(stressed_scores)/len(stressed_scores)
    print("\n平均异常分：NORMAL %.2f（n=%d） vs 压力场景 %.2f（n=%d）"%(
        avg_normal,len(normal_scores),avg_stressed,len(stressed_scores)))
    assert avg_stressed>avg_normal
