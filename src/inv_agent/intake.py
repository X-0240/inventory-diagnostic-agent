"""入口层：把自然语言诉求解析成结构化参数，并做实体解析与幻觉拒绝。

设计要点（这是项目二原来完全缺的一环：触发一直是定时任务，从结构化 facts 出发）：
- 模型只负责"抽取意图与实体名"，**不允许它直接给出 sku_id/period 这类主键**
  （模型会编造主键，所以主键一律由数据库解析得出）
- 实体解析交给数据库：精确编号 → 名称唯一匹配 → 多候选（AMBIGUOUS）→ 无匹配（NOT_FOUND）
- 相对时间（本周/上周）相对"数据里最新的周期"解析，而不是相对系统当天
  （本环境数据是 2011 年，用系统当天会解析到一个不存在的周期）
"""

import json
import os
import re
from datetime import timedelta

from inv_agent import config, db, llm, pipeline

INTENTS = ("CHECK_STOCK", "PLAN_REPLENISH", "EXPLAIN_DECISION")
REQUIRED_FIELDS = ("intent", "sku_query", "period_hint")
# 商品编号形态：字母前缀 + 数字，或 4-6 位纯数字（避免把年份、周期里的数字当成编号）
ENTITY_RE = re.compile(
    r"(?<![A-Za-z0-9])([A-Z]{1,4}-?\d{3,8}|(?<!\d)\d{4,6}(?!\d))"
)


def strip_time_expr(text):
    """去掉时间表达，避免把 2011-W21 里的年份当商品编号。"""
    out = re.sub(r"\d{4}-W\d{1,2}", " ", text or "", flags=re.I)
    return re.sub(r"上上周|上周|本周|这周|下周|下个周期", " ", out)


def count_entities(text):
    return sorted(set(ENTITY_RE.findall(strip_time_expr(text))))


SYSTEM_PROMPT = """你是库存诉求解析器。把用户的话解析成结构化参数，只输出 JSON，字段名一字不改：
{
  "intent": "<CHECK_STOCK|PLAN_REPLENISH|EXPLAIN_DECISION>",
  "sku_query": "<用户提到的商品编号或商品名，原样抽取，不要自己编造编号>",
  "period_hint": "<用户提到的时间，如 2011-W21 / 上周 / 本周；没提到就填空字符串>"
}
约束：不要输出 sku_id、不要计算数量、不要替用户做决定；
如果用户没有明确指某个商品，sku_query 填他原话里最像商品的部分；确实没有就填空字符串。
意图判定规则（按这个判，不要凭感觉）：
- CHECK_STOCK：只想知道库存/覆盖情况，例如"库存多少""够不够卖""有没有异常"
- PLAN_REPLENISH：明确要生成或加入补货计划，例如"要不要补货""加进补货计划""下单"
- EXPLAIN_DECISION：问依据、原因或处置结果，例如"为什么这么判""依据是什么""处理结果" """


def validate(raw):
    errors = []
    for f in REQUIRED_FIELDS:
        if f not in raw:
            errors.append("缺字段: " + f)
    if raw.get("intent") not in INTENTS:
        errors.append("意图不在枚举内: " + str(raw.get("intent")))
    if not isinstance(raw.get("sku_query"), str):
        errors.append("sku_query 必须是字符串")
    return {"ok": not errors, "errors": errors}


def latest_period():
    """数据里可用的最新周期（由销量表推导），相对时间都相对它解析。"""
    row = db.query_one("SELECT MAX(sale_date) AS d FROM sales_daily")
    if not row or not row["d"]:
        return None
    return pipeline.period_of(row["d"])


def shift_period(period, weeks):
    start, _ = pipeline.period_bounds(period)
    return pipeline.period_of(start + timedelta(weeks=weeks))


def resolve_period(hint):
    """支持显式 ISO 周与三类相对表达；解析不了就落到最新周期。"""
    base = latest_period()
    if not base:
        return None, "NOT_FOUND"
    text = (hint or "").strip()
    if not text:
        return base, "DEFAULTED"
    m = re.search(r"(\d{4})-W(\d{1,2})", text, re.I)
    if m:
        return "%04d-W%02d" % (int(m.group(1)), int(m.group(2))), "EXPLICIT"
    if "上上" in text:
        return shift_period(base, -2), "RELATIVE"
    if "上周" in text or "上个周期" in text:
        return shift_period(base, -1), "RELATIVE"
    if "下周" in text or "下个周期" in text:
        return shift_period(base, 1), "RELATIVE"
    if "本周" in text or "这周" in text or "这个周期" in text:
        return base, "RELATIVE"
    return base, "DEFAULTED"


def resolve_sku(query):
    """实体解析：精确编号 → 名称唯一匹配 → 多候选 → 无匹配。主键只从这里来。"""
    q = (query or "").strip()
    if not q:
        return {"status": "NOT_FOUND", "reason": "没提到商品"}
    exact = db.query_one(
        "SELECT id,sku_code,name FROM sku WHERE sku_code=%s", (q,)
    )
    if exact:
        return {"status": "OK", "sku": exact, "matched_by": "code"}
    rows = db.query_all(
        "SELECT id,sku_code,name FROM sku WHERE name LIKE %s OR sku_code LIKE %s LIMIT 6",
        ("%" + q + "%", "%" + q + "%"),
    )
    if len(rows) == 1:
        return {"status": "OK", "sku": rows[0], "matched_by": "name"}
    if len(rows) > 1:
        return {
            "status": "AMBIGUOUS",
            "candidates": [
                {"sku_code": r["sku_code"], "name": r["name"]} for r in rows
            ],
        }
    return {
        "status": "NOT_FOUND",
        "reason": "数据库里没有这个商品（很可能是模型编造的实体）",
    }


class StubParser:
    """离线解析器：只按固定规则抽取，用于测试与无 key 环境，不参与任何能力结论。"""

    name = "stub"

    def complete_json(self, system, user):
        text = json.loads(user)["text"]
        intent = "CHECK_STOCK"
        # 规则与 SYSTEM_PROMPT 里的意图判定规则保持一致，避免桩和提示词两套标准
        if any(
            k in text
            for k in ("补货", "下单", "采购", "加进", "要不要补", "需要补")
        ):
            intent = "PLAN_REPLENISH"
        if any(
            k in text
            for k in (
                "为什么",
                "怎么判",
                "依据",
                "解释",
                "处理结果",
                "处置依据",
            )
        ):
            intent = "EXPLAIN_DECISION"
        # 时间表达：显式 ISO 周优先，其次相对表达；"上上周" 必须排在 "上周" 之前判断
        hint = ""
        explicit = re.search(r"\d{4}-W\d{1,2}", text, re.I)
        if explicit:
            hint = explicit.group(0)
        else:
            for token in ("上上周", "上周", "本周", "这周", "下周", "下个周期"):
                if token in text:
                    hint = token
                    break
        # 先剥掉时间表达再抽商品编号，否则"2011-W21"里的年份会被当成商品号
        stripped = strip_time_expr(text)
        code = ENTITY_RE.search(stripped)
        return {
            "intent": intent,
            "sku_query": code.group(1) if code else "",
            "period_hint": hint,
        }


def parse(text, client=None):
    """返回 {ok, code, intent, params{sku_code,period}, sku, candidates}；错误码与契约一致。"""
    client = client or get_parser()
    try:
        raw = client.complete_json(
            SYSTEM_PROMPT, json.dumps({"text": text}, ensure_ascii=False)
        )
    except Exception as e:
        return {
            "ok": False,
            "code": "UPSTREAM_TIMEOUT",
            "message": str(e)[:120],
        }
    check = validate(raw)
    if not check["ok"]:
        return {
            "ok": False,
            "code": "SCHEMA_INVALID",
            "errors": check["errors"],
        }
    # 一次请求提到多个商品时不允许猜：要么让用户分开问，要么走人工确认
    # 多实体判定看原文（去时间表达后），不能只看模型抽出来的 sku_query——桩只填第一个编号
    entity_hits = count_entities(text)
    if len(entity_hits) >= 2:
        return {
            "ok": False,
            "code": "AMBIGUOUS",
            "intent": raw["intent"],
            "entities": entity_hits,
            "message": "一次请求提到多个商品（{}），请分开问或指定其一".format(
                "、".join(entity_hits)
            ),
        }
    sku_result = resolve_sku(raw.get("sku_query", ""))
    if sku_result["status"] == "AMBIGUOUS":
        return {
            "ok": False,
            "code": "AMBIGUOUS",
            "intent": raw["intent"],
            "candidates": sku_result["candidates"],
            "message": "匹配到多个商品，需要人确认",
        }
    if sku_result["status"] == "NOT_FOUND":
        return {
            "ok": False,
            "code": "NOT_FOUND",
            "intent": raw["intent"],
            "message": sku_result.get("reason", "未匹配到商品"),
            "raw": raw,
        }
    period, how = resolve_period(raw.get("period_hint", ""))
    if not period:
        return {"ok": False, "code": "NOT_FOUND", "message": "没有可用周期"}
    return {
        "ok": True,
        "code": "OK",
        "intent": raw["intent"],
        "params": {"sku_code": sku_result["sku"]["sku_code"], "period": period},
        "sku": {
            "id": sku_result["sku"]["id"],
            "sku_code": sku_result["sku"]["sku_code"],
            "name": sku_result["sku"]["name"],
        },
        "matched_by": sku_result["matched_by"],
        "period_source": how,
        "parser": getattr(client, "name", "llm"),
    }


def get_parser():
    """入口层的解析器：强制走桩或没配 key 时用规则解析器，否则用真实模型。

    注意不要复用 llm.get_client()：那是诊断节点的客户端，提示词与输出 schema 都不同。
    """
    if os.getenv("LLM_FORCE_STUB") == "1":
        return StubParser()
    if config.LLM_API_KEY and config.LLM_BASE_URL and config.LLM_MODEL:
        return llm.OpenAICompatLLM(
            config.LLM_BASE_URL, config.LLM_API_KEY, config.LLM_MODEL
        )
    return StubParser()


def answer(text, client=None):
    """入口层落地动作：解析成功就针对该 SKU 跑一次计划（走图，与批量同一条路径）。"""
    result = parse(text, client=client)
    if not result["ok"]:
        return result
    if result["intent"] == "CHECK_STOCK":
        from inv_agent import graph

        run = graph.run(
            result["params"]["sku_code"],
            result["params"]["period"],
            mode="plan",
            diagnose=True,
        )
        result["suggestion_id"] = run.get("suggestion_id")
        result["errors"] = run.get("errors", [])
        return result
    return result  # PLAN_REPLENISH / EXPLAIN_DECISION 由调用方决定后续动作
