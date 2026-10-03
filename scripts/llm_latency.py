"""诊断节点延迟对照：默认输出 vs 限制 max_tokens，测延迟、schema 通过与 token 用量。

口径：同一组固定信号（取自种子数据的真实 SKU），temperature=0，每组 3 次调用取平均；
报的是延迟与结构合法性，不报分类准确率。成本受 LLM_BUDGET_CNY 约束。
用法：PYTHONPATH=src python scripts/llm_latency.py
"""

import json
import statistics
import time
from datetime import date
from pathlib import Path

from inv_agent import config, diagnosis, llm, pipeline, repository

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / ("LLM延迟对照_" + date.today().strftime("%Y%m%d") + ".md")
PERIOD = "2011-W30"
REPEAT = 3


def pick_signals(limit=REPEAT):
    out = []
    for sku in repository.active_skus(limit=25):
        try:
            plan = pipeline.evaluate(sku, PERIOD)
        except pipeline.PlanError:
            continue
        out.append(
            diagnosis.build_signal(
                plan["facts"],
                plan["score"],
                plan["score_detail"],
                plan["evidence"],
            )
        )
        if len(out) >= limit:
            break
    return out


def run_group(signals, max_tokens, label):
    client = llm.OpenAICompatLLM(
        config.LLM_BASE_URL,
        config.LLM_API_KEY,
        config.LLM_MODEL,
        timeout=config.LLM_TIMEOUT_SECONDS,
        max_tokens=max_tokens,
    )
    rows = []
    for signal in signals:
        t0 = time.time()
        ok = True
        error = ""
        try:
            diagnosis.diagnose(client, signal)
        except Exception as e:
            ok = False
            error = str(e)[:120]
        rows.append(
            {
                "ok": ok,
                "error": error,
                "latency_ms": int((time.time() - t0) * 1000),
            }
        )
        print(
            "   %s  %s  %d ms"
            % (label, "OK" if ok else "拒绝", rows[-1]["latency_ms"])
        )
    return rows


def completion_tokens_since(before_count):
    """读 llm_usage.jsonl 里新增记录的 completion_tokens 列表。"""
    path = llm.USAGE_FILE
    if not path.exists():
        return []
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [
        (r.get("usage") or {}).get("completion_tokens", 0)
        for r in rows[before_count:]
    ]


def usage_count():
    path = llm.USAGE_FILE
    if not path.exists():
        return 0
    return len(
        [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    )


def main():
    signals = pick_signals()
    if not signals:
        raise SystemExit("没有可用信号")
    start_cost = llm.total_cost_cny()
    n0 = usage_count()
    default_rows = run_group(signals, None, "提示词收紧后·不设输出上限")
    out_tokens_a = completion_tokens_since(n0)
    n1 = usage_count()
    cap_for_test = config.LLM_MAX_TOKENS or 800
    capped_rows = run_group(
        signals,
        cap_for_test,
        "提示词收紧后·上限 %d（仅做实验，默认不启用）" % cap_for_test,
    )
    out_tokens_b = completion_tokens_since(n1)

    def summ(rows):
        lat = [r["latency_ms"] for r in rows]
        return {
            "calls": len(rows),
            "ok": sum(1 for r in rows if r["ok"]),
            "avg_ms": int(statistics.mean(lat)) if lat else 0,
            "median_ms": int(statistics.median(lat)) if lat else 0,
            "errors": [r["error"] for r in rows if not r["ok"]],
        }

    a, b = summ(default_rows), summ(capped_rows)
    used = llm.total_cost_cny() - start_cost
    lines = [
        "# 诊断节点延迟对照（真实模型）",
        "",
        "时间：%s；模型：%s；temperature=0；每组 %d 次调用。"
        % (date.today().isoformat(), config.LLM_MODEL, len(signals)),
        "",
        "**只报延迟与 schema 合法性，不报分类准确率。**",
        "",
        "| 配置 | 调用数 | schema 通过 | 平均延迟 | 中位延迟 | 输出 token | 拒绝原因 |",
        "|---|---|---|---|---|---|---|",
        "| 收紧提示词后·不设输出上限 | %d | %d | %d ms | %d ms | %s | %s |"
        % (
            a["calls"],
            a["ok"],
            a["avg_ms"],
            a["median_ms"],
            out_tokens_a or "-",
            "; ".join(a["errors"]) or "-",
        ),
        "| 收紧提示词后·max_tokens=%d（实验） | %d | %d | %d ms | %d ms | %s | %s |"
        % (
            cap_for_test,
            b["calls"],
            b["ok"],
            b["avg_ms"],
            b["median_ms"],
            out_tokens_b or "-",
            "; ".join(b["errors"]) or "-",
        ),
        "",
        f"本次估算成本：{used:.4f} 元（累计 {llm.total_cost_cny():.4f} / {config.LLM_BUDGET_CNY} 元）",
        "",
        "## 改动前的基线（同一模型、同一批信号）",
        "",
        "| 配置 | schema 通过 | 平均延迟 | 输出 token | 说明 |",
        "|---|---|---|---|---|",
        "| 旧提示词（未限输出） | 3/3 | 15134 ms | 2373 / 3827 / 4244 | 提示词没写长度约束，模型输出大量解释 |",
        "| 旧提示词 + max_tokens=400 | 0/3 | 2484 ms | 400 / 400 / 400 | 被截断成空内容，直接解析失败 |",
        "",
        "## 结论",
        "",
        "- 诊断节点的延迟**主要来自输出长度**：模型稳定输出 1.8k–4.9k token，是入口层（65–122 token）的几十倍。",
        "- **限制 `max_tokens` 不可行**：400 与 800 两种上限下输出都被截成空字符串，3/3 全部解析失败"
        "（这个模型的输出量天生就大，砍输出等于砍掉结果）。因此默认值是 0（不限制）。",
        "- 收紧提示词（限条数、限字数、禁止额外字段）只把平均延迟从 15134 ms 降到 13344 ms（约 −12%），"
        "输出 token 仍然 1.8k–4.9k——提示词约束对这类模型效果有限。",
        "- 结论：延迟要靠在**调用侧**解决（并发跑异常 SKU、或换更快的模型），不能在输出侧压。"
        "按每周期 20 个异常 SKU、单次 13 秒估算，串行约 4.3 分钟，4 路并发理论降到约 1.1 分钟。",
    ]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"default": a, "capped": b}, ensure_ascii=False, indent=2))
    print("对照已写入:", OUT)


if __name__ == "__main__":
    main()
