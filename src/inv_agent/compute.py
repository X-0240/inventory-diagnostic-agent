"""确定性计算层：基线需求估算、安全库存、(s,S)、经济订货批量。

设计约束（契约 10.3）：这些函数的返回值是补货数量的唯一来源，LLM 不得改写。
全部为纯函数，输入输出均为数值，便于单测与手算对照。
"""

import math

# 标准正态分位数（服务水平→z）。用表加线性插值，避免引入 scipy
Z_TABLE = [
    (0.500, 0.000),
    (0.800, 0.842),
    (0.850, 1.036),
    (0.900, 1.282),
    (0.950, 1.645),
    (0.975, 1.960),
    (0.990, 2.326),
    (0.995, 2.576),
    (0.999, 3.090),
]


def z_for_service_level(service_level):
    if service_level <= Z_TABLE[0][0]:
        return Z_TABLE[0][1]
    for (p0, z0), (p1, z1) in zip(Z_TABLE, Z_TABLE[1:]):
        if service_level <= p1:
            ratio = (service_level - p0) / (p1 - p0)
            return round(z0 + (z1 - z0) * ratio, 3)
    return Z_TABLE[-1][1]


def moving_average(series, window):
    """基线需求估算：最近 window 天销量均值。空序列返回 0。"""
    if not series:
        return 0.0
    tail = series[-window:] if window and window > 0 else series
    return sum(tail) / len(tail)


def stdev(series):
    """样本标准差；少于 2 个点返回 0（新品无历史时的边界）。"""
    n = len(series)
    if n < 2:
        return 0.0
    mean = sum(series) / n
    var = sum((x - mean) ** 2 for x in series) / (n - 1)
    return math.sqrt(var)


def median(values):
    if not values:
        return 0.0
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def winsorize(series, lower_pct=0.05, upper_pct=0.95):
    """按分位裁掉极端值：批发型数据里个别超大订单会把 σ 抬到不可用。"""
    if not series:
        return []
    ordered = sorted(series)
    n = len(ordered)
    low = ordered[max(0, min(n - 1, int(n * lower_pct)))]
    high = ordered[max(0, min(n - 1, int(n * upper_pct) - 1))]
    return [min(max(x, low), high) for x in series]


def robust_sigma(series, intermittent_ratio=0.3):
    """稳健离散度：间歇需求用"发生-规模"模型，其余用 MAD 估计。

    - 间歇需求（有销量天数 < 30%）：σ ≈ 单次规模 × √发生概率
    - 其余：σ = 1.4826 × MAD；MAD 为 0 时退回截尾样本标准差
    """
    if not series:
        return 0.0
    non_zero = [x for x in series if x > 0]
    if len(non_zero) < max(3, intermittent_ratio * len(series)):
        p = len(non_zero) / len(series)
        size = (sum(non_zero) / len(non_zero)) if non_zero else 0.0
        return size * math.sqrt(p)
    med = median(series)
    mad = median([abs(x - med) for x in series])
    sigma = 1.4826 * mad
    if sigma > 0:
        return sigma
    return stdev(winsorize(series))


def demand_sigma(series, baseline, floor_ratio=0.1):
    """实际使用的 σ：稳健估计 + 下限，避免极端裁剪后退化成 0 导致安全库存归零。"""
    sigma = robust_sigma(series)
    if sigma <= 0:
        sigma = stdev(winsorize(series))
    return max(sigma, abs(baseline) * floor_ratio)


def safety_stock(z, sigma, lead_time_days):
    """安全库存 = ceil(z × σ × √交期)。"""
    lt = max(lead_time_days, 0)
    return math.ceil(z * sigma * math.sqrt(lt))


def reorder_point(baseline_daily, lead_time_days, safety_qty):
    """再订货点 = 交期需求 + 安全库存。"""
    return math.ceil(baseline_daily * lead_time_days + safety_qty)


def target_qty(baseline_daily, lead_time_days, review_days, z, sigma):
    """(s,S) 里的 S：保护期（交期 + 复核周期）的需求 + 该保护期内的波动缓冲。

    只用"单周期均值"作目标，在销量波动大的批发型数据上会欠库存，所以这里用保护期长度。
    """
    protection = max(lead_time_days, 0) + max(review_days, 0)
    return math.ceil(
        baseline_daily * protection + z * sigma * math.sqrt(protection)
    )


def eoq(annual_demand, order_cost, unit_cost, holding_rate):
    """经济订货批量 sqrt(2DK/hC)；参数非法时返回 0。"""
    denom = holding_rate * unit_cost
    if denom <= 0 or annual_demand <= 0 or order_cost <= 0:
        return 0.0
    return math.sqrt(2 * annual_demand * order_cost / denom)


def round_up_to_pack(qty, pack_size, moq):
    """按最小起订量与包装倍数向上取整。"""
    qty = max(qty, moq)
    if pack_size and pack_size > 1:
        qty = math.ceil(qty / pack_size) * pack_size
    return int(qty)


def suggest_qty(
    inventory_position,
    reorder_point_qty,
    target_qty_value,
    moq,
    pack_size,
    eoq_qty=0.0,
    cap_level=None,
):
    """补货量：IP ≤ ROP 时补到 S；低于经济批量则取经济批量；最后受库存水平上限约束。

    inventory_position = 可用库存 + 交期内到货（不含未到货的在途）
    cap_level = 下单后允许达到的最高库存水平（不是下单量上限）
    """
    if inventory_position > reorder_point_qty:
        return 0
    need = target_qty_value - inventory_position
    if need <= 0:
        return 0
    raw = need
    if eoq_qty and raw < eoq_qty:
        raw = math.ceil(eoq_qty)
    if cap_level is not None:
        allowed = cap_level - inventory_position
        if allowed <= 0:
            return 0
        raw = min(raw, allowed)
    if raw <= 0:
        return 0
    qty = round_up_to_pack(raw, pack_size, moq)
    # 受上限约束时，包装倍数必须向下取整，否则向上取整会把库存顶过上限（差几个单位的假违规）
    if cap_level is not None and inventory_position + qty > cap_level:
        room = int(cap_level - inventory_position)
        step = pack_size if pack_size and pack_size > 1 else 1
        qty = (room // step) * step if room > 0 else 0
        if qty < moq:
            return 0
    return qty


def impact_amount(qty, unit_cost):
    """影响金额：用于异常排序与审批分级。"""
    return round(qty * unit_cost, 2)
