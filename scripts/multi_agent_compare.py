"""多 Agent 对照实验：单 Agent（现状） vs 双 Agent（证据收集 + 归因）。

要回答的问题只有一个：**多加一个 Agent，有没有换来证据质量或结论质量的增量？**

口径（写死）：
- 双臂跑同一批 SKU、同一周期、同一个模型、temperature=0
- A 臂＝现在的生产路径（一次调用出结论）
- B 臂＝先让「证据收集 Agent」挑关键证据（一次调用），再让「归因 Agent」基于证据包出结论（一次调用）
- 指标：每 SKU 调用次数、总 token、平均单次延迟、schema 通过、**证据覆盖族数**（结论里引用到的工具族：
  get_inventory / get_sales_series / compute_policy，0–3）、双臂结论一致率
- 只报这些结构性指标，**不报分类准确率**（没有 ground truth，期望结论也是我按规则写的）
- 结论不写进生产路径：双 Agent 只在本脚本里实现，用来给「为什么不用多 Agent」提供证据

用法：PYTHONPATH=src python scripts/multi_agent_compare.py [--skus 10] [--stub]
"""

import argparse
import json
import statistics
import time
from datetime import date
from pathlib import Path

from inv_agent import config, diagnosis, llm, pipeline, repository

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / ("多Agent对照_" + date.today().strftime("%Y%m%d") + ".md")
PERIOD = "2011-W30"
FAMILIES = ("get_inventory", "get_sales_series", "compute_policy")

EVIDENCE_PROMPT = """你是证据收集器。输入是量化信号与候选证据。请选出支撑归因最关键的证据（最多 4 条），
并指出还缺哪类证据。只输出 JSON，字段名一字不改：
{
  "selected": [{"tool": "<工具名>", "summary": "<不超过 20 字>", "why": "<不超过 15 字>"}],
  "gaps": ["<缺失的证据类型，最多 3 条>"]
}
约束：只能从输入的候选证据里选，不要新增事实；不要输出任何数量或执行决定。"""


def build_signals(limit):
    out = []
    for sku in repository.active_skus(limit=max(limit * 3, 30)):
        try:
            plan = pipeline.evaluate(sku, PERIOD)
        except pipeline.PlanError:
            continue
        signal = diagnosis.build_signal(
            plan["facts"], plan["score"], plan["score_detail"], plan["evidence"]
        )
        out.append({"sku": sku["sku_code"], "signal": signal})
        if len(out) >= limit:
            break
    return out


def usage_snapshot():
    path = llm.USAGE_FILE
    if not path.exists():
        return 0, []
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return len(rows), rows


def tokens_since(count_before):
    count, rows = usage_snapshot()
    comp = [
        (r.get("usage") or {}).get("completion_tokens", 0)
        for r in rows[count_before:]
    ]
    prompt = [
        (r.get("usage") or {}).get("prompt_tokens", 0)
        for r in rows[count_before:]
    ]
    return {
        "calls": count - count_before,
        "completion": sum(comp),
        "prompt": sum(prompt),
    }


def coverage(result):
    """结论里引用到的工具族数（0–3）：衡量证据是否只盯着一个侧面。"""
    tools = {
        ref.get("tool")
        for ref in result.get("evidence_refs", [])
        if isinstance(ref, dict)
    }
    return len(tools & set(FAMILIES))


def arm_single(client, signal):
    t0 = time.time()
    result = diagnosis.diagnose(client, signal)
    return {
        "result": result,
        "latency_ms": int((time.time() - t0) * 1000),
        "ok": True,
    }


def arm_two_agent(client, signal):
    t0 = time.time()
    pack = client.complete_json(
        EVIDENCE_PROMPT, json.dumps(signal, ensure_ascii=False)
    )
    merged = dict(signal)
    merged["evidence_pack"] = pack
    result = diagnosis.diagnose(client, merged)
    return {
        "result": result,
        "latency_ms": int((time.time() - t0) * 1000),
        "ok": True,
        "selected": len(pack.get("selected", [])),
        "gaps": len(pack.get("gaps", [])),
    }


def run_arm(name, fn, signals):
    rows = []
    for item in signals:
        before, _ = usage_snapshot()
        try:
            out = fn(item["signal"])
            tokens = tokens_since(before)
            rows.append(
                {
                    "sku": item["sku"],
                    "ok": True,
                    "hypothesis": out["result"]["hypothesis_type"],
                    "coverage": coverage(out["result"]),
                    "evidence_count": len(
                        out["result"].get("evidence_refs", [])
                    ),
                    "latency_ms": out["latency_ms"],
                    **tokens,
                }
            )
        except Exception as e:
            tokens = tokens_since(before)
            rows.append(
                {
                    "sku": item["sku"],
                    "ok": False,
                    "error": str(e)[:100],
                    "coverage": 0,
                    "evidence_count": 0,
                    "latency_ms": 0,
                    **tokens,
                }
            )
        r = rows[-1]
        print(
            "   %-6s %-10s %-4s 调用%s 输出%s token 覆盖%d族 %d ms"
            % (
                name,
                r["sku"],
                "OK" if r["ok"] else "拒绝",
                r["calls"],
                r["completion"],
                r["coverage"],
                r["latency_ms"],
            )
        )
    return rows


def summarize(rows):
    ok = [r for r in rows if r["ok"]]
    return {
        "skus": len(rows),
        "ok": len(ok),
        "failed": len(rows) - len(ok),
        "calls_total": sum(r["calls"] for r in rows),
        "completion_tokens_total": sum(r["completion"] for r in rows),
        "coverage_mean": (
            round(statistics.mean([r["coverage"] for r in rows]), 2)
            if rows
            else 0
        ),
        "coverage_full3": sum(1 for r in rows if r["coverage"] >= 3),
        "latency_mean_s": (
            round(statistics.mean([r["latency_ms"] for r in rows]) / 1000, 1)
            if rows
            else 0
        ),
        "hypotheses": {
            h: [
                r["hypothesis"] for r in rows if r.get("hypothesis") == h
            ].__len__()
            for h in {r.get("hypothesis") for r in rows if r.get("hypothesis")}
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skus", type=int, default=10)
    parser.add_argument(
        "--stub", action="store_true", help="用确定性桩跑一遍检查链路（不花钱）"
    )
    args = parser.parse_args()
    if args.stub:
        client = llm.StubLLM()
    else:
        client = llm.get_client()
        if isinstance(client, llm.StubLLM):
            raise SystemExit("没有配置真实模型；要跑链路自检请加 --stub")
    signals = build_signals(args.skus)
    if not signals:
        raise SystemExit("没有可用信号")
    print(
        "SKU 数:",
        len(signals),
        "| 模型:",
        config.LLM_MODEL if not args.stub else "stub",
    )
    start_cost = llm.total_cost_cny()
    # 同批次 SKU 分别跑两臂：先 A 后 B，保证输入完全一致
    # 用 lambda 绑定 client：直接传函数名会丢掉 client 参数（这里踩过一次，10 个 SKU 全在入参就抛错）
    rows_a = run_arm("A单", lambda s: arm_single(client, s), signals)
    rows_b = run_arm("B双", lambda s: arm_two_agent(client, s), signals)
    used = llm.total_cost_cny() - start_cost
    a, b = summarize(rows_a), summarize(rows_b)
    same = sum(
        1
        for ra, rb in zip(rows_a, rows_b)
        if ra.get("hypothesis") == rb.get("hypothesis")
    )
    lines = [
        "# 多 Agent 对照实验（单 Agent vs 证据收集+归因）",
        "",
        "时间：%s；模型：%s；temperature=0；样本 %d 个 SKU（周期 %s）。"
        % (
            date.today().isoformat(),
            config.LLM_MODEL if not args.stub else "stub",
            len(signals),
            PERIOD,
        ),
        "",
        "口径：A 臂＝生产路径（一次调用出结论）；B 臂＝先证据收集（一次调用）再归因（一次调用）。",
        "只报结构性指标，不报分类准确率（没有 ground truth）。判定人与口径即本文件。",
        "",
        "| 指标 | A 臂（单 Agent） | B 臂（双 Agent） |",
        "|---|---|---|",
        "| SKU 数 | %d | %d |" % (a["skus"], b["skus"]),
        "| 失败数 | %d | %d |" % (a["failed"], b["failed"]),
        "| 总调用次数 | %d | %d |" % (a["calls_total"], b["calls_total"]),
        "| 输出 token 合计 | %d | %d |"
        % (a["completion_tokens_total"], b["completion_tokens_total"]),
        "| 每次调用平均延迟 | {:.1f} 秒 | {:.1f} 秒 |".format(
            a["latency_mean_s"] / max(1, a["calls_total"] / max(1, a["skus"])),
            b["latency_mean_s"] / max(1, b["calls_total"] / max(1, b["skus"])),
        ),
        "| 每 SKU 平均耗时 | {:.1f} 秒 | {:.1f} 秒 |".format(
            a["latency_mean_s"], b["latency_mean_s"]
        ),
        "| 证据覆盖族数（均值，满分 3） | {:.2f} | {:.2f} |".format(
            a["coverage_mean"], b["coverage_mean"]
        ),
        "| 三条事实族全覆盖的 SKU | %d | %d |"
        % (a["coverage_full3"], b["coverage_full3"]),
        "| 两臂结论一致率 | — | %d/%d |" % (same, len(signals)),
        "",
        f"本次估算成本：{used:.4f} 元（累计 {llm.total_cost_cny():.4f} / {config.LLM_BUDGET_CNY} 元）",
        "",
        "## 结论",
        "",
        "- 看三件事：B 臂多花的调用与 token、B 臂的覆盖族数是否更高、两臂结论是否一致。",
        "- 如果 B 臂覆盖族数没有提高、结论一致率又很高，那「多加一个 Agent」就只增加了成本与故障面，"
        "这正是本项目选单 Agent 的证据；反之则说明该拆。",
    ]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"A": a, "B": b, "same_hypothesis": same},
            ensure_ascii=False,
            indent=2,
        )
    )
    print("对照已写入:", OUT)


if __name__ == "__main__":
    main()
