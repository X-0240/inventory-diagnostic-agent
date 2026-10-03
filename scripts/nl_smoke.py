"""用真实模型跑一遍冻结的自然语言用例集（入口层冒烟）。

口径：用例与期望值见 `src/inv_agent/nl_cases.py`（期望值由我按种子数据手写，不是抽样标注）。
报"通过 X/N"，这是功能用例通过率，**不是模型能力评测**；成本受 LLM_BUDGET_CNY 约束。
用法：PYTHONPATH=src python scripts/nl_smoke.py
"""

import json
import statistics
import time
from datetime import date
from pathlib import Path

from inv_agent import config, intake, llm
from inv_agent.nl_cases import CASES, resolve_marker

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / ("NL入口冒烟_" + date.today().strftime("%Y%m%d") + ".md")


def check(case, result, latest):
    expect = case["expect"]
    if result["code"] != expect["code"]:
        return False, "期望 {}，实际 {}".format(expect["code"], result["code"])
    if expect["code"] != "OK":
        return True, ""
    want = resolve_marker(expect["period"], latest, intake.shift_period)
    got = result["params"]
    if got["sku_code"] != expect["sku_code"] or got["period"] != want:
        return False, "期望 {}@{}，实际 {}@{}".format(
            expect["sku_code"],
            want,
            got["sku_code"],
            got["period"],
        )
    if result["intent"] != expect["intent"]:
        return False, "意图期望 {}，实际 {}".format(
            expect["intent"],
            result["intent"],
        )
    return True, ""


def main():
    client = intake.get_parser()
    if isinstance(client, intake.StubParser):
        raise SystemExit(
            "当前是规则解析器（无 key 或 LLM_FORCE_STUB=1），冒烟需要真实模型"
        )
    latest = intake.latest_period()
    start_cost = llm.total_cost_cny()
    rows = []
    for case in CASES:
        t0 = time.time()
        try:
            result = intake.parse(case["text"], client=client)
            ok, why = check(case, result, latest)
        except Exception as e:
            result = {"code": "EXCEPTION", "message": str(e)[:120]}
            ok, why = False, "异常: " + str(e)[:80]
        rows.append(
            {
                "text": case["text"],
                "ok": ok,
                "why": why,
                "code": result.get("code"),
                "params": result.get("params"),
                "latency_ms": int((time.time() - t0) * 1000),
            }
        )
        print(
            "  %-24s %-4s %s"
            % (
                (
                    "「" + case["text"][:14] + "…」"
                    if len(case["text"]) > 14
                    else "「" + case["text"] + "」"
                ),
                "OK" if ok else "FAIL",
                why,
            )
        )
    used = llm.total_cost_cny() - start_cost
    passed = [r for r in rows if r["ok"]]
    lat = [r["latency_ms"] for r in rows]
    lines = [
        "# NL 入口冒烟（真实模型）",
        "",
        f"时间：{date.today().isoformat()}；模型：{config.LLM_MODEL}；temperature=0。",
        "",
        "**这是功能用例通过率，不是模型能力评测**：期望值由我按种子数据手写，样本 10 条。",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        "| 用例数 | %d |" % len(rows),
        "| 通过 | %d |" % len(passed),
        "| 平均延迟 | %d ms |" % (statistics.mean(lat) if lat else 0),
        f"| 本次估算成本 | {used:.4f} 元 |",
        f"| 累计成本 | {llm.total_cost_cny():.4f} / {config.LLM_BUDGET_CNY} 元 |",
        "",
        "## 明细",
        "",
        "| 输入 | 结果 | 说明 | 解析出的参数 | 延迟 |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            "| %s | %s | %s | %s | %d ms |"
            % (
                r["text"],
                "通过" if r["ok"] else "失败",
                r["why"] or "-",
                (
                    json.dumps(r["params"], ensure_ascii=False)
                    if r["params"]
                    else "-"
                ),
                r["latency_ms"],
            )
        )
    lines += [
        "",
        "## 边界说明",
        "",
        "- 主键（sku_id/period）一律由数据库解析，模型只抽取意图与实体名；模型编造编号会被实体解析拒掉。",
        "- 相对时间（本周/上周）相对「数据里最新周期」解析，而不是系统当天——本环境数据是 2011 年。",
    ]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        "\n通过 %d/%d；平均延迟 %d ms；本次成本 %.4f 元"
        % (len(passed), len(rows), statistics.mean(lat) if lat else 0, used)
    )
    print("结果已写入:", OUT)


if __name__ == "__main__":
    main()
