"""held-out 使用纪律：阈值调过的场景不得进入 held-out。"""

import pytest

from inv_agent.scenarios import (
    HOLDOUT_EXCLUDED,
    load_scenarios,
    split_for_scenario,
)


def _scenarios():
    mapping = load_scenarios()
    if not mapping:
        pytest.skip("缺少 data/scenarios.json")
    return mapping


def test_adjusted_scenarios_are_not_in_holdout():
    scenarios = _scenarios()
    for scenario in HOLDOUT_EXCLUDED:
        codes = [c for c, s in scenarios.items() if s == scenario]
        dev, held_out = split_for_scenario(scenario, codes)
        assert held_out == [], (
            "场景 %s 阈值调过，不应出现在 held-out" % scenario
        )
        assert sorted(dev) == sorted(codes)


def test_other_scenarios_still_have_holdout():
    scenarios = _scenarios()
    codes = [c for c, s in scenarios.items() if s == "PROMOTION_SURGE"]
    _, held_out = split_for_scenario("PROMOTION_SURGE", codes)
    assert held_out, "未调整过阈值的场景应保留 held-out 样本"
