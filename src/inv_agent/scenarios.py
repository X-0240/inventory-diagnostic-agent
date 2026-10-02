"""场景规格与 held-out 纪律：测试与脚本共用这一份定义，避免两处各写一套。"""

import json

from inv_agent import config

# 每个场景的观察周期与"设计信号"判定；可见性门槛统一 50%（构造数据的可见下限，不是识别率）
SCENARIO_SPEC = {
    "PROMOTION_SURGE": {
        "period": "2011-W11",
        "signal": "surge_ratio>=1.2",
        "min_ratio": 0.5,
    },
    "SUPPLIER_DELAY": {
        "period": "2011-W30",
        "signal": "leadtime_bias_days>=7",
        "min_ratio": 0.5,
    },
    # 新品第 140 天（约 W20）才开始有销量，所以看 W21
    "NEW_PRODUCT_NO_HISTORY": {
        "period": "2011-W21",
        "signal": "days_with_sales<7",
        "min_ratio": 0.5,
    },
    "STOCKOUT_CASCADE": {
        "period": "2011-W30",
        "signal": "coverage_days<=3",
        "min_ratio": 0.5,
    },
    # 需求上移从第 105 天开始；近 28 天窗口要基本落在上移之后，W19 才对齐
    "DEMAND_SHIFT": {
        "period": "2011-W19",
        "signal": "shift_ratio>=1.2",
        "min_ratio": 0.5,
    },
    "DATA_ANOMALY": {
        "period": "2011-W30",
        "signal": "zero_days_7>=2",
        "min_ratio": 0.5,
    },
}

# 阈值是观察后调整过的场景，按裁定不得进入 held-out，只能留在开发/回归集
HOLDOUT_EXCLUDED = {"DEMAND_SHIFT"}


def load_scenarios():
    path = config.DATA_DIR / "scenarios.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def split_for_scenario(scenario, codes):
    """调参集 / held-out 各半（排序后奇偶分，可复现）；被排除的场景整体进开发集。"""
    ordered = sorted(codes)
    if scenario in HOLDOUT_EXCLUDED:
        return ordered, []
    return ordered[0::2], ordered[1::2]


def period_for(scenario):
    return SCENARIO_SPEC.get(scenario, {}).get("period")
