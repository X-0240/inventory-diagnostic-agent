"""异常归因：受限动作空间 + schema 校验；枚举外输出一律拒绝（SCHEMA_INVALID）。

LLM 只能"选假设、组证据、给建议、判是否转人工"，不产出数量、不改策略、不驱动执行。
"""
import json

from inv_agent.anomaly import HYPOTHESES

ACTION_ENUM=("ADVANCE","SPLIT","REPLAN","HUMAN")
REQUIRED_FIELDS=("hypothesis_type","evidence_refs","conflicting_evidence",
                 "proposed_actions","needs_human","confidence")

SYSTEM_PROMPT=("你是库存异常归因器。只允许在给定假设枚举内选择，必须列出证据引用，"
               "不得输出任何数量、价格或执行动作的决定权。返回严格 JSON。")

def build_signal(facts,score,detail,evidence):
    """给 LLM 的输入：量化信号 + 证据引用，不含自由文本污染。

    字段一律用 .get() 兜底：信号构造失败会拖垮整轮批量计划，不能让缺键变成系统级故障。
    """
    sku=facts.get("sku") or {}
    return {
        "sku_id":sku.get("id"),
        "period":facts.get("period"),
        "score":score,
        "factors":detail,
        "baseline_daily":facts.get("baseline_daily",0.0),
        "sigma":facts.get("sigma",0.0),
        "sample_days":len(facts.get("qty_series") or []),
        "days_with_sales":facts.get("days_with_sales",0),
        "coverage_days":facts.get("coverage_days",0.0),
        "missing_days":facts.get("missing_days",0),
        "recent7_avg":facts.get("recent7_avg",0.0),
        "recent28_avg":facts.get("recent28_avg",0.0),
        "prior28_avg":facts.get("prior28_avg",0.0),
        "zero_days_7":facts.get("zero_days_7",0),
        "zero_days_28":facts.get("zero_days_28",0),
        "leadtime_bias_days":facts.get("leadtime_bias_days",0.0),
        "available":facts.get("available",0),
        "inbound_due":facts.get("inbound_due",0),
        "reorder_point":facts.get("reorder_point",0),
        "target_qty":facts.get("target_qty",0),
        "candidate_hypotheses":list(HYPOTHESES),
        "evidence":evidence,
    }

def validate_diagnosis(obj):
    """结构校验：缺字段、枚举外、类型错都算不合法，调用方按 SCHEMA_INVALID 处理。"""
    errors=[]
    for field in REQUIRED_FIELDS:
        if field not in obj:
            errors.append("缺少字段: "+field)
    if obj.get("hypothesis_type") not in HYPOTHESES:
        errors.append("假设类型不在枚举内: "+str(obj.get("hypothesis_type")))
    if not isinstance(obj.get("evidence_refs"),list) or not obj.get("evidence_refs"):
        errors.append("evidence_refs 必须是非空列表")
    else:
        for ref in obj["evidence_refs"]:
            if not isinstance(ref,dict) or "tool" not in ref or "summary" not in ref:
                errors.append("证据引用缺 tool 或 summary")
                break
    if not isinstance(obj.get("proposed_actions"),list) or not obj.get("proposed_actions"):
        errors.append("proposed_actions 必须是非空列表")
    else:
        for act in obj["proposed_actions"]:
            if act.get("action") not in ACTION_ENUM:
                errors.append("动作不在枚举内: "+str(act.get("action")))
                break
            if not act.get("rationale"):
                errors.append("proposed_actions 缺 rationale")
                break
    try:
        conf=float(obj.get("confidence"))
        if conf<0 or conf>1:
            errors.append("confidence 必须在 0-1")
    except (TypeError,ValueError):
        errors.append("confidence 不是数值")
    if not isinstance(obj.get("needs_human"),bool):
        errors.append("needs_human 必须是布尔")
    return {"ok":not errors,"errors":errors}

def diagnose(client,signal):
    """调模型并校验；不合法时抛 ValueError，调用方转 SCHEMA_INVALID 并记审计。"""
    raw=client.complete_json(SYSTEM_PROMPT,json.dumps(signal,ensure_ascii=False))
    result=validate_diagnosis(raw)
    if not result["ok"]:
        raise ValueError("诊断输出不合法: "+"; ".join(result["errors"]))
    return raw
