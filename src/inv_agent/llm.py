"""LLM 客户端：默认 Stub（离线可跑、结果可复现），配了 key 才走真实接口。

契约 10.3：LLM 只能调用只读与计算类工具，输出必须是受限 schema。
"""

import json
import os
import time
import urllib.error
import urllib.request

from inv_agent import config


class LLMError(RuntimeError):
    pass


USAGE_FILE = config.DATA_DIR / "llm_usage.jsonl"


def today_calls():
    """今日已调用次数：用于成本护栏，超上限直接停。"""
    if not USAGE_FILE.exists():
        return 0
    today = time.strftime("%Y-%m-%d", time.gmtime())
    count = 0
    for line in USAGE_FILE.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("date") == today:
            count += 1
    return count


def total_cost_cny():
    """累计估算成本（元）：按记录的 token 用量与配置单价计算，用于预算护栏。"""
    if not USAGE_FILE.exists():
        return 0.0
    total = 0.0
    for line in USAGE_FILE.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        usage = rec.get("usage") or {}
        total += (
            usage.get("prompt_tokens", 0) / 1_000_000
        ) * config.LLM_PRICE_IN_CNY_PER_1M
        total += (
            usage.get("completion_tokens", 0) / 1_000_000
        ) * config.LLM_PRICE_OUT_CNY_PER_1M
    return round(total, 4)


def log_usage(model, prompt_chars, usage=None):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    rec = {
        "date": time.strftime("%Y-%m-%d", time.gmtime()),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": model,
        "prompt_chars": prompt_chars,
        "usage": usage,
    }
    with USAGE_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


class LLMClient:
    def complete_json(self, system, user):
        raise NotImplementedError


class StubLLM(LLMClient):
    """确定性桩：把量化信号映射成假设类型，用于测试与无 key 环境。

    它不做"自由发挥"，只按固定优先级选一个假设并给出证据引用，
    目的是让整条链路在没有外部依赖时也能端到端跑通与评估。
    """

    name = "stub"

    def complete_json(self, system, user):
        signal = json.loads(user)
        factors = signal.get("factors", {})
        evidence = signal.get("evidence", [])
        # 优先级：历史不足 → 需求类信号（突增/上移）→ 供应类信号（延迟/缺货）→ 数据断点兜底
        if signal.get("days_with_sales", 0) < 7:
            hypothesis = "NEW_PRODUCT_NO_HISTORY"
        elif factors.get("surge", 0) > 0:
            hypothesis = "PROMOTION_SURGE"
        elif factors.get("shift", 0) > 0:
            hypothesis = "DEMAND_SHIFT"
        elif factors.get("leadtime", 0) >= 1.0:
            hypothesis = "SUPPLIER_DELAY"
        elif (
            factors.get("stockout", 0) >= 1.0 or factors.get("coverage", 0) > 0
        ):
            hypothesis = "STOCKOUT_CASCADE"
        elif factors.get("zeros", 0) > 0:
            hypothesis = "DATA_ANOMALY"
        else:
            hypothesis = "DATA_ANOMALY"
        severity = min(1.0, max(0.2, signal.get("score", 0) / 6.0))
        return {
            "hypothesis_type": hypothesis,
            "evidence_refs": evidence,
            "conflicting_evidence": bool(signal.get("missing_days", 0)),
            "proposed_actions": [
                {
                    "action": "ADVANCE",
                    "rationale": "证据指向单一主因，按规则路径补货",
                },
                {
                    "action": "HUMAN",
                    "rationale": "金额或覆盖天数触及阈值时转人工",
                },
            ],
            "needs_human": signal.get("score", 0)
            >= config.ANOMALY_ENTER_SCORE * 1.5,
            "confidence": round(0.55 + 0.4 * severity, 3),
            "engine": self.name,
        }


class OpenAICompatLLM(LLMClient):
    """OpenAI 兼容的 /chat/completions，用标准库实现，少一个依赖。"""

    name = "openai-compat"

    def __init__(self, base_url, api_key, model, timeout=60, max_tokens=None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens

    def complete_json(self, system, user):
        if today_calls() >= config.LLM_DAILY_CALL_BUDGET:
            raise LLMError(
                "RATE_LIMITED: 今日调用已达上限 "
                + str(config.LLM_DAILY_CALL_BUDGET)
                + " 次"
            )
        if total_cost_cny() >= config.LLM_BUDGET_CNY:
            raise LLMError(
                "RATE_LIMITED: 累计成本已达上限 "
                + str(config.LLM_BUDGET_CNY)
                + " 元"
            )
        body = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if self.max_tokens:
            body["max_tokens"] = self.max_tokens
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.api_key,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise LLMError("LLM HTTP " + str(e.code)) from e
        except Exception as e:
            raise LLMError("LLM 调用失败: " + type(e).__name__) from e
        text = payload["choices"][0]["message"]["content"]
        log_usage(self.model, len(system) + len(user), payload.get("usage"))
        return json.loads(text)


def get_client():
    """有 key 走真实接口，否则用桩；两种实现返回同一 schema。

    LLM_FORCE_STUB=1 时即使配了 key 也走桩：跑演示数据或回归测试不该烧钱、也不该等十几秒。
    """
    if os.getenv("LLM_FORCE_STUB") == "1":
        return StubLLM()
    if config.LLM_API_KEY and config.LLM_BASE_URL and config.LLM_MODEL:
        return OpenAICompatLLM(
            config.LLM_BASE_URL,
            config.LLM_API_KEY,
            config.LLM_MODEL,
            timeout=config.LLM_TIMEOUT_SECONDS,
            max_tokens=config.LLM_MAX_TOKENS or None,
        )
    return StubLLM()
