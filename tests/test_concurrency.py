"""并发批量计划的机制验证：用注入延迟代替真实模型，比较串行与并发的整体耗时。

口径：把诊断节点的模型调用替换成固定延时（0.3 秒），跑同一批 SKU；
串行耗时≈SKU 数×延时，4 路并发应接近四分之一。这不是模型延迟评测，只是验证并发机制生效。
"""

import time

from inv_agent import config, graph

PERIOD_A = "2011-W26"
PERIOD_B = "2011-W27"
DELAY = 0.3
LIMIT = 12


def _run_with_delay(period, workers, monkeypatch):
    def slow_diagnose(client, signal, max_retry=1):
        time.sleep(DELAY)
        return {
            "hypothesis_type": "DEMAND_SHIFT",
            "evidence_refs": [
                {
                    "tool": "get_sales_series",
                    "summary": "注入延迟",
                    "timestamp": "t",
                }
            ],
            "conflicting_evidence": False,
            "proposed_actions": [
                {"action": "ADVANCE", "rationale": "机制验证"}
            ],
            "needs_human": False,
            "confidence": 0.5,
        }

    monkeypatch.setenv("LLM_FORCE_STUB", "1")
    # 把"连续确认"降到 1 个周期，否则新周期里没有 SKU 会被确认，注入的延迟根本不会执行
    monkeypatch.setattr(config, "ANOMALY_CONFIRM_DAYS", 1)
    monkeypatch.setattr(graph.diagnosis, "diagnose", slow_diagnose)
    t0 = time.time()
    result = graph.run_period(
        period, limit=LIMIT, quota=999, force=True, workers=workers
    )
    return time.time() - t0, result


def _diagnosed_count(result):
    stats = result.get("stats", {})
    return stats.get("confirmed_diagnose", 0)


def _cleanup(period):
    from inv_agent import db

    with db.tx() as cur:
        cur.execute(
            "DELETE FROM replenishment_suggestion WHERE period=%s", (period,)
        )
        cur.execute("DELETE FROM exception_case WHERE period=%s", (period,))
        cur.execute("DELETE FROM policy_snapshot WHERE period=%s", (period,))
        cur.execute(
            "DELETE FROM approval_record WHERE suggestion_id NOT IN "
            "(SELECT id FROM replenishment_suggestion)"
        )


def test_workers_reduce_wall_time(db_ready, monkeypatch):
    seq_seconds, _ = _run_with_delay(PERIOD_A, 1, monkeypatch)
    par_seconds, _ = _run_with_delay(PERIOD_B, 4, monkeypatch)
    print(
        "\n串行 %.1f 秒（%d 个 SKU） vs 4 路并发 %.1f 秒"
        % (seq_seconds, LIMIT, par_seconds)
    )
    try:
        # 并发最多等到串行的 70%：留出线程与数据库开销的余量
        assert (
            par_seconds < seq_seconds * 0.7
        ), "并发没有生效：串行 %.1fs vs 并发 %.1fs" % (seq_seconds, par_seconds)
    finally:
        _cleanup(PERIOD_A)
        _cleanup(PERIOD_B)
