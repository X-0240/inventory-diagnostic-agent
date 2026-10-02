"""异常归因：受限动作空间 + schema 校验；枚举外输出一律拒绝（SCHEMA_INVALID）。

LLM 只能"选假设、组证据、给建议、判是否转人工"，不产出数量、不改策略、不驱动执行。
"""

import json

from inv_agent.anomaly import HYPOTHESES

ACTION_ENUM = ("ADVANCE", "SPLIT", "REPLAN", "HUMAN")
REQUIRED_FIELDS = (
    "hypothesis_type",
    "evidence_refs",
    "conflicting_evidence",
    "proposed_actions",
    "needs_human",
    "confidence",
)

SYSTEM_PROMPT = """你是库存异常归因器，只做三件事：在假设枚举里选一个、列出证据引用、给出给审批人看的建议。
必须严格按下面的 JSON 结构输出，字段名一字不改，不要输出 JSON 以外的任何内容：
{
  "hypothesis_type": "<PROMOTION_SURGE|NEW_PRODUCT_NO_HISTORY|SUPPLIER_DELAY|STOCKOUT_CASCADE|DEMAND_SHIFT|DATA_ANOMALY>",
  "evidence_refs": [{"tool": "<工具名>", "summary": "<一句话证据>", "timestamp": "<ISO8601>"}],
  "conflicting_evidence": <true 或 false>,
  "proposed_actions": [{"action": "<ADVANCE|SPLIT|REPLAN|HUMAN>", "rationale": "<一句话理由>"}],
  "needs_human": <true 或 false>,
  "confidence": <0 到 1 之间的数>
}
约束：补货数量、价格、是否真正执行都不由你决定；evidence_refs 至少 1 条；
只能引用输入里给出的因素与证据，不要新增事实。
输出要求（非常重要，直接影响延迟）：
- 最多 3 条 evidence_refs，每条 summary 不超过 30 个字
- 最多 3 条 proposed_actions，每条 rationale 不超过 20 个字
- 不要输出 schema 之外的任何字段（不要 reasoning、不要解释段落、不要 markdown 代码块）
- 整体输出控制在 400 token 以内"""


def build_signal(facts, score, detail, evidence):
    """给 LLM 的输入：量化信号 + 证据引用，不含自由文本污染。

    字段一律用 .get() 兜底：信号构造失败会拖垮整轮批量计划，不能让缺键变成系统级故障。
    """
    sku = facts.get("sku") or {}
    return {
        "sku_id": sku.get("id"),
        "period": facts.get("period"),
        "score": score,
        "factors": detail,
        "baseline_daily": facts.get("baseline_daily", 0.0),
        "sigma": facts.get("sigma", 0.0),
        "sample_days": len(facts.get("qty_series") or []),
        "days_with_sales": facts.get("days_with_sales", 0),
        "coverage_days": facts.get("coverage_days", 0.0),
        "missing_days": facts.get("missing_days", 0),
        "recent7_avg": facts.get("recent7_avg", 0.0),
        "recent28_avg": facts.get("recent28_avg", 0.0),
        "prior28_avg": facts.get("prior28_avg", 0.0),
        "zero_days_7": facts.get("zero_days_7", 0),
        "zero_days_28": facts.get("zero_days_28", 0),
        "leadtime_bias_days": facts.get("leadtime_bias_days", 0.0),
        "available": facts.get("available", 0),
        "inbound_due": facts.get("inbound_due", 0),
        "reorder_point": facts.get("reorder_point", 0),
        "target_qty": facts.get("target_qty", 0),
        "candidate_hypotheses": list(HYPOTHESES),
        "evidence": evidence,
    }


def validate_diagnosis(obj):
    """结构校验：缺字段、枚举外、类型错都算不合法，调用方按 SCHEMA_INVALID 处理。"""
    errors = []
    for field in REQUIRED_FIELDS:
        if field not in obj:
            errors.append("缺少字段: " + field)
    if obj.get("hypothesis_type") not in HYPOTHESES:
        errors.append("假设类型不在枚举内: " + str(obj.get("hypothesis_type")))
    if not isinstance(obj.get("evidence_refs"), list) or not obj.get(
        "evidence_refs"
    ):
        errors.append("evidence_refs 必须是非空列表")
    else:
        for ref in obj["evidence_refs"]:
            if (
                not isinstance(ref, dict)
                or "tool" not in ref
                or "summary" not in ref
            ):
                errors.append("证据引用缺 tool 或 summary")
                break
    if not isinstance(obj.get("proposed_actions"), list) or not obj.get(
        "proposed_actions"
    ):
        errors.append("proposed_actions 必须是非空列表")
    else:
        for act in obj["proposed_actions"]:
            if act.get("action") not in ACTION_ENUM:
                errors.append("动作不在枚举内: " + str(act.get("action")))
                break
            if not act.get("rationale"):
                errors.append("proposed_actions 缺 rationale")
                break
    try:
        conf = float(obj.get("confidence"))
        if conf < 0 or conf > 1:
            errors.append("confidence 必须在 0-1")
    except (TypeError, ValueError):
        errors.append("confidence 不是数值")
    if not isinstance(obj.get("needs_human"), bool):
        errors.append("needs_human 必须是布尔")
    return {"ok": not errors, "errors": errors}


def diagnose(client, signal, max_retry=1):
    """调模型并校验；按契约给 SCHEMA_INVALID 一次自修复机会，仍不合法才抛错。

    第一次失败的原因会追加到提示词里，让模型自己修字段名——这比直接放弃更符合契约的重试上限（1 次）。
    """
    last_error = ""
    for attempt in range(max_retry + 1):
        prompt = SYSTEM_PROMPT
        if attempt > 0:
            prompt += (
                "\n上一次输出不合法，错误：%s\n请只修正字段名与结构后重新输出完整 JSON。"
                % last_error
            )
        raw = client.complete_json(
            prompt, json.dumps(signal, ensure_ascii=False)
        )
        result = validate_diagnosis(raw)
        if result["ok"]:
            return raw
        last_error = "; ".join(result["errors"])
    raise ValueError("诊断输出不合法: " + last_error)
