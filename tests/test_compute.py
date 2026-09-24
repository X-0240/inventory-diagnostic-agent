"""计算层单测：手算对照 + 契约要求的边界（σ=0、交期=0、MOQ 大于需求量、销量全 0）。"""
import math

import pytest

from inv_agent import compute

def test_z_table_and_interpolation():
    assert compute.z_for_service_level(0.5)==0.0
    assert compute.z_for_service_level(0.95)==1.645
    assert compute.z_for_service_level(0.9)==1.282
    assert compute.z_for_service_level(0.96)==pytest.approx(1.771,abs=1e-3)
    assert compute.z_for_service_level(0.9999)==3.09

def test_moving_average_boundaries():
    assert compute.moving_average([],28)==0.0
    assert compute.moving_average([0,0,0],28)==0.0
    assert compute.moving_average([1,2,3],2)==2.5

def test_stdev_handles_single_point():
    assert compute.stdev([2,2,2])==0.0
    assert compute.stdev([1,2,3])==pytest.approx(1.0)
    assert compute.stdev([5])==0.0
    assert compute.stdev([])==0.0

def test_safety_stock_zero_sigma_and_zero_leadtime():
    assert compute.safety_stock(1.645,0,7)==0
    assert compute.safety_stock(1.645,10,0)==0
    assert compute.safety_stock(1.645,10,9)==50

def test_reorder_point_hand_computed():
    assert compute.reorder_point(2.0,7,10)==24
    assert compute.reorder_point(0,0,0)==0

def test_target_qty_hand_computed():
    #保护期 = 交期7 + 复核7 = 14；需求 2×14=28；缓冲 1.645×1×√14=6.155
    assert compute.target_qty(2.0,7,7,1.645,1.0)==35
    assert compute.target_qty(0,0,0,1.645,0)==0

def test_eoq_hand_computed():
    value=compute.eoq(3650,80,10,0.22)
    assert value==pytest.approx(math.sqrt(2*3650*80/(0.22*10)),rel=1e-6)
    assert compute.eoq(0,80,10,0.22)==0.0
    assert compute.eoq(3650,0,10,0.22)==0.0
    assert compute.eoq(3650,80,10,0)==0.0

def test_round_up_to_pack_moq_dominates():
    assert compute.round_up_to_pack(7,5,10)==10
    assert compute.round_up_to_pack(12,5,1)==15
    assert compute.round_up_to_pack(3,1,1)==3

def test_suggest_qty_rules():
    #IP 高于再订货点：不下单
    assert compute.suggest_qty(25,20,50,1,1)==0
    #IP 低于再订货点：补到目标
    assert compute.suggest_qty(10,20,50,1,1)==40
    #低于经济批量时取经济批量
    assert compute.suggest_qty(10,20,50,1,1,eoq_qty=100)==100
    #受库存水平上限约束（上限约束的是下单后的库存水平，不是下单量）
    assert compute.suggest_qty(10,20,200,1,1,cap_level=70)==60
    #库存已经到上限：不再下单
    assert compute.suggest_qty(10,20,200,1,1,cap_level=10)==0
    #目标已经低于当前：不下单
    assert compute.suggest_qty(10,20,5,1,1)==0

def test_impact_amount_rounding():
    assert compute.impact_amount(3,10.005)==30.02

def test_median_helper():
    assert compute.median([])==0.0
    assert compute.median([3])==3
    assert compute.median([1,2,3,4])==2.5

def test_robust_sigma_resists_outlier():
    #21 天里 20 天卖 10、1 天卖 10 万：MAD 为 0，但不应让 σ 被那个极值抬高
    series=[10]*20+[100000]
    assert compute.robust_sigma(series)==0.0
    #样本标准差会被这个极值拉爆，作为对照
    assert compute.stdev(series)>20000

def test_robust_sigma_intermittent_model():
    #20 天里只有 3 天有销量、单次规模 100：发生概率 0.15 → σ ≈ 100×√0.15
    series=[0]*17+[100]*3
    assert compute.robust_sigma(series)==pytest.approx(100*math.sqrt(0.15),rel=1e-6)

def test_demand_sigma_has_floor():
    baseline=10.0
    sigma=compute.demand_sigma([10]*20+[100000],baseline)
    assert sigma==pytest.approx(1.0)   #下限 = 均值 × 10%

def test_winsorize_trims_tails():
    series=[1,2,3,4,1000]
    out=compute.winsorize(series)
    assert max(out)<1000
    assert len(out)==len(series)
