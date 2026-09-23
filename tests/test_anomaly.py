"""异常分界单测：五项分数、双阈值滞回、连续确认、配额排序。"""
from inv_agent import anomaly

def test_demand_zscore_with_sigma():
    #近 7 天日均 10，基线 5，σ=2 → (10-5)/2 = 2.5
    assert anomaly.demand_zscore(70,7,5.0,2.0)==2.5

def test_demand_zscore_without_sigma_uses_relative_threshold():
    assert anomaly.demand_zscore(0,7,0.0,0.0)==0.0
    assert anomaly.demand_zscore(70,7,0.0,0.0)==3.0
    assert anomaly.demand_zscore(70,7,5.0,0.0)==pytest_approx(3.333)

def pytest_approx(value):
    import pytest
    return pytest.approx(value,abs=1e-3)

def test_coverage_days_boundaries():
    assert anomaly.coverage_days(0,0,5.0)==0.0
    assert anomaly.coverage_days(10,5,5.0)==3.0
    assert anomaly.coverage_days(10,5,0.0)==999.0

def test_anomaly_score_only_counts_positive_side():
    score,detail=anomaly.anomaly_score({"demand":2.0,"coverage":-1.0,"flags":1.0})
    assert score==3.0
    assert detail["coverage"]==0.0

def test_decide_anomaly_needs_confirmation_then_hysteresis():
    active,streak=anomaly.decide_anomaly(3.5,False,3.0,2.0,0,2)
    assert (active,streak)==(False,1)
    active,streak=anomaly.decide_anomaly(3.5,False,3.0,2.0,1,2)
    assert (active,streak)==(True,2)
    #已激活：低于退出阈值才关闭
    assert anomaly.decide_anomaly(2.5,True,3.0,2.0,5,2)==(True,5)
    assert anomaly.decide_anomaly(1.5,True,3.0,2.0,5,2)==(False,0)
    #未激活且低于进入阈值：确认计数清零
    assert anomaly.decide_anomaly(1.0,False,3.0,2.0,1,2)==(False,0)

def test_apply_quota_keeps_top_by_impact_amount():
    cands=[{"sku":"A","impact_amount":10},{"sku":"B","impact_amount":50},{"sku":"C","impact_amount":30}]
    selected,skipped=anomaly.apply_quota(cands,2)
    assert [c["sku"] for c in selected]==["B","C"]
    assert [c["sku"] for c in skipped]==["A"]
