"""真实模型冒烟验证：在冻结场景上跑一遍，记录延迟、token、schema 通过与拒绝。

口径与边界（写死）：
- 这是冒烟验证，不是评估：不报分类准确率，因为期望结论由我自己按规则推导，样本量也小。
- temperature=0；每次调用都过 schema 校验；被拒次数如实记录（含一次自修复重试后的结果）。
- 预算由 .env 的 LLM_BUDGET_CNY 控制，超限会直接抛错。
- held-out 纪律：阈值调整过的场景（见 tests/test_scenarios.py 的 HOLDOUT_EXCLUDED）不参与。

用法：PYTHONPATH=src python scripts/llm_smoke.py
"""
import json
import statistics
import time
from datetime import date
from pathlib import Path

from inv_agent import config, diagnosis, llm, pipeline, repository
from inv_agent.scenarios import HOLDOUT_EXCLUDED, load_scenarios, period_for, split_for_scenario

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/"docs"/("LLM冒烟验证_"+date.today().strftime("%Y%m%d")+".md")
PER_SCENARIO=2   #每个场景取多少个 held-out 样本

def samples_per_scenario():
    mapping=load_scenarios()
    if not mapping:
        raise SystemExit("缺少 data/scenarios.json，先构建数据集")
    out={}
    for scenario in ("PROMOTION_SURGE","SUPPLIER_DELAY","NEW_PRODUCT_NO_HISTORY",
                     "STOCKOUT_CASCADE","DATA_ANOMALY","NORMAL"):
        if scenario in HOLDOUT_EXCLUDED:
            continue
        codes=[c for c,s in mapping.items() if s==scenario]
        if not codes:
            continue
        _,held_out=split_for_scenario(scenario,codes)
        out[scenario]=held_out[:PER_SCENARIO]
    return out

def main():
    client=llm.get_client()
    if isinstance(client,llm.StubLLM):
        raise SystemExit("当前是确定性桩（没有配置 LLM key），冒烟验证需要真实模型")
    print("模型:",config.LLM_MODEL,"| 预算: %s 元 | 已用: %.4f 元"%(config.LLM_BUDGET_CNY,llm.total_cost_cny()))
    started_cost=llm.total_cost_cny()
    rows=[]
    for scenario,codes in samples_per_scenario().items():
        period=period_for(scenario)
        for code in codes:
            sku=repository.db.query_one("SELECT * FROM sku WHERE sku_code=%s",(code,))
            if not sku:
                continue
            plan=pipeline.evaluate(sku,period)
            signal=diagnosis.build_signal(plan["facts"],plan["score"],plan["score_detail"],plan["evidence"])
            t0=time.time()
            record={"scenario":scenario,"sku":code,"period":period,"score":plan["score"]}
            try:
                out=diagnosis.diagnose(client,signal)     #内含一次 schema 自修复
                record.update({"ok":True,"hypothesis":out["hypothesis_type"],
                               "confidence":float(out["confidence"]),
                               "needs_human":out["needs_human"],
                               "evidence_count":len(out["evidence_refs"])})
            except Exception as e:
                record.update({"ok":False,"error":str(e)[:160]})
            record["latency_ms"]=int((time.time()-t0)*1000)
            rows.append(record)
            print("  %-24s %-10s %-4s %.0f ms"%(scenario,code,"OK" if record["ok"] else "拒绝",record["latency_ms"]))
    used=llm.total_cost_cny()-started_cost
    ok=[r for r in rows if r["ok"]]
    rejected=[r for r in rows if not r["ok"]]
    latencies=[r["latency_ms"] for r in rows]
    lines=["# 真实模型冒烟验证","",
           "时间：%s；模型：%s；temperature=0。"%(date.today().isoformat(),config.LLM_MODEL),
           "",
           "**这是冒烟验证，不是评估**：不报分类准确率。期望结论由我按规则推导、样本量小，",
           "只能证明「真实模型在受限 schema 下能稳定产出可用结构」，不能证明模型好坏。",
           "",
           "| 指标 | 值 |","|---|---|",
           "| 调用样本数 | %d |"%len(rows),
           "| schema 通过 | %d |"%len(ok),
           "| schema 拒绝（含一次自修复后仍失败） | %d |"%len(rejected),
           "| 平均延迟 | %d ms |"%(statistics.mean(latencies) if latencies else 0),
           "| 延迟中位数 | %d ms |"%(statistics.median(latencies) if latencies else 0),
           "| 本次估算成本 | %.4f 元 |"%used,
           "| 累计估算成本 | %.4f 元（上限 %s 元） |"%(llm.total_cost_cny(),config.LLM_BUDGET_CNY),
           "",
           "## 逐条明细","",
           "| 场景 | SKU | 异常分 | 结果 | 假设 | 置信 | 需人工 | 证据条数 | 延迟 |",
           "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append("| %s | %s | %.2f | %s | %s | %s | %s | %s | %d ms |"%(
            r["scenario"],r["sku"],r["score"],"通过" if r["ok"] else "拒绝",
            r.get("hypothesis","-"),("%.2f"%r["confidence"]) if "confidence" in r else "-",
            r.get("needs_human","-"),r.get("evidence_count","-"),r["latency_ms"]))
    lines+=["","## 已知边界","",
            "- 样本量为每场景 2 个 held-out SKU，只够做链路冒烟；要谈质量必须先定义样本量、期望结论与标注人。",
            "- `DEMAND_SHIFT` 因阈值调整过，已按要求排除在冒烟与 held-out 之外。",
            "- 成本按 .env 里的单价估算（设定值，需按官方价目核对）。"]
    OUT.parent.mkdir(parents=True,exist_ok=True)
    OUT.write_text("\n".join(lines)+"\n",encoding="utf-8")
    print("\n冒烟结果已写入:",OUT)
    print("通过 %d / 拒绝 %d；平均延迟 %d ms；本次成本 %.4f 元"%(len(ok),len(rejected),
          statistics.mean(latencies) if latencies else 0,used))

if __name__=="__main__":
    main()
