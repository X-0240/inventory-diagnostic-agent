"""异常分界：五项异常分 + 双阈值滞回 + 连续确认 + 每日配额。

契约要求分界必须可审计、可版本化，所以这里只做纯计算，阈值全部来自 config_version。
"""

HYPOTHESES = (
    "PROMOTION_SURGE",
    "NEW_PRODUCT_NO_HISTORY",
    "SUPPLIER_DELAY",
    "STOCKOUT_CASCADE",
    "DEMAND_SHIFT",
    "DATA_ANOMALY",
)


def demand_zscore(recent_qty, days, baseline_daily, sigma):
    """需求突变：近 days 天日均与基线的偏离，按标准差归一；σ=0 时用相对阈值兜底。"""
    if days <= 0:
        return 0.0
    recent = recent_qty / days
    if sigma and sigma > 0:
        return (recent - baseline_daily) / sigma
    if baseline_daily <= 0:
        return 3.0 if recent > 0 else 0.0
    return (recent - baseline_daily) / max(baseline_daily * 0.3, 1e-6)


def coverage_days(available, inbound_due, baseline_daily):
    """库存覆盖天数；基线为 0 时返回 999 表示不构成缺货风险。"""
    if baseline_daily <= 0:
        return 999.0
    return (available + inbound_due) / baseline_daily


def leadtime_bias(expected_days, actual_days, sigma_days):
    """交期偏差：实际比承诺晚多少天，按供应商历史偏差归一。"""
    diff = actual_days - expected_days
    if sigma_days and sigma_days > 0:
        return diff / sigma_days
    return float(diff)


def anomaly_score(factors, weights=None):
    """加权总分：只累加正向偏离（低于预期才异常），返回总分与分项明细。"""
    w = weights or {
        "demand": 1.0,
        "coverage": 1.0,
        "leadtime": 1.0,
        "supplier": 1.0,
        "flags": 1.0,
    }
    score = 0.0
    detail = {}
    for name, value in factors.items():
        contribution = max(float(value), 0.0) * w.get(name, 1.0)
        detail[name] = round(contribution, 3)
        score += contribution
    return round(score, 3), detail


def decide_anomaly(
    score, prev_active, enter, exit_, confirm_streak, confirm_days
):
    """双阈值滞回 + 连续确认：进入用高阈值，退出用低阈值，避免边界抖动。"""
    if prev_active:
        if score < exit_:
            return False, 0
        return True, confirm_streak
    if score >= enter:
        streak = confirm_streak + 1
        if streak >= confirm_days:
            return True, streak
        return False, streak
    return False, 0


def apply_quota(candidates, quota):
    """每日配额：按影响金额降序取前 quota 个进 Agent 调查，其余走规则路径。"""
    ordered = sorted(
        candidates, key=lambda c: c.get("impact_amount", 0), reverse=True
    )
    return ordered[:quota], ordered[quota:]


def coverage_factor(coverage, threshold_days=7.0):
    """覆盖天数低于阈值时转成正向分值，越低越大。"""
    if coverage >= threshold_days:
        return 0.0
    return (threshold_days - coverage) / threshold_days * 2.0


def stockout_factor(coverage):
    """缺货信号：覆盖天数 ≤1 天算强缺货，≤3 天算弱缺货。"""
    if coverage <= 1.0:
        return 2.0
    if coverage <= 3.0:
        return 1.0
    return 0.0


def level_ratio(recent, prior):
    """水平比：区分短期突增（近 7 天 vs 近 28 天）与需求上移（近 28 天 vs 前 28 天）。"""
    if prior <= 0:
        return 1.0 if recent <= 0 else 3.0
    return recent / prior


def ratio_factor(ratio, threshold):
    """比值超过阈值才计分，超出幅度归一化。"""
    if ratio <= threshold:
        return 0.0
    return min((ratio - threshold) / threshold, 2.0)


def zeros_factor(zero_days_7, zero_days_28):
    """数据断点：近 7 天零销量比例明显高于近 28 天，视为数据异常而非真实需求。"""
    if zero_days_7 < 2:
        return 0.0
    r7 = zero_days_7 / 7.0
    r28 = zero_days_28 / 28.0
    if r7 > max(r28 * 1.5, r28 + 0.15):
        return 1.5
    return 0.0
