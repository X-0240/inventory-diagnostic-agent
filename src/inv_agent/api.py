"""FastAPI 薄层：把 CLI 的能力暴露成 HTTP 接口，不重复实现任何业务逻辑。

鉴权说明（诚实标注）：当前用请求头 X-Actor / X-Role 传身份，只做权限矩阵校验，
没有真正的认证与令牌体系；接入真实系统时必须替换成认证中间件。
启动：PYTHONPATH=src uvicorn inv_agent.api:app --port 8100
"""

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from inv_agent import (
    config,
    executor,
    graph,
    intake,
    metrics,
    pipeline,
    repository,
)

ROLES = ("BUYER", "APPROVER", "SUPERVISOR", "ADMIN")

app = FastAPI(
    title="库存诊断 Agent API",
    version="0.1.0",
    description="补货建议的生成、审批、执行与复算（权限矩阵校验版）",
)


class ActorContext(BaseModel):
    actor: str
    role: str


def current_actor(
    x_actor: str | None = Header(default=None),
    x_role: str | None = Header(default=None),
) -> ActorContext:
    if not x_actor or not x_role:
        raise HTTPException(
            status_code=401, detail="缺少 X-Actor / X-Role 请求头"
        )
    if x_role not in ROLES:
        raise HTTPException(
            status_code=401, detail="角色不在允许集合内: " + x_role
        )
    return ActorContext(actor=x_actor, role=x_role)


class Decision(BaseModel):
    decision: str = Field(pattern="^(APPROVE|REJECT|MODIFY)$")
    qty: int | None = None
    comment: str | None = None


class PlanRequest(BaseModel):
    period: str
    limit: int | None = None
    quota: int | None = None
    force: bool = False
    workers: int = 1


class IntakeRequest(BaseModel):
    text: str
    run: bool = False  # True 时解析成功后直接跑一次计划（会落库）


@app.get("/health")
def health():
    return {
        "status": "ok",
        "warehouse": config.WAREHOUSE_ID,
        "rule_version": config.RULE_VERSION,
    }


@app.get("/suggestions")
def list_suggestions(
    status: str | None = None,
    period: str | None = None,
    limit: int = 50,
    actor: ActorContext = Depends(current_actor),
):
    rows = repository.list_suggestions(
        status=status, period=period, limit=limit
    )
    return {
        "count": len(rows),
        "items": [
            {
                "id": r["id"],
                "sku": r["sku_code"],
                "period": r["period"],
                "qty": r["qty"],
                "amount": float(r["amount"]),
                "status": r["status"],
                "hypothesis": r["hypothesis_type"],
                "confidence": (
                    float(r["confidence"])
                    if r["confidence"] is not None
                    else None
                ),
                "content_hash": r["content_hash"][:12],
            }
            for r in rows
        ],
    }


@app.get("/suggestions/{suggestion_id}")
def get_suggestion(
    suggestion_id: int, actor: ActorContext = Depends(current_actor)
):
    row = repository.get_suggestion(suggestion_id)
    if not row:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    out = {
        k: row[k]
        for k in (
            "id",
            "sku_id",
            "period",
            "qty",
            "amount",
            "status",
            "hypothesis_type",
            "confidence",
            "content_hash",
            "version",
        )
    }
    out["amount"] = float(row["amount"])
    out["confidence"] = (
        float(row["confidence"]) if row["confidence"] is not None else None
    )
    return out


@app.post("/suggestions/{suggestion_id}/approve")
def approve(
    suggestion_id: int,
    body: Decision,
    actor: ActorContext = Depends(current_actor),
):
    """审批与（可选）执行：权限矩阵与状态机校验都在 graph.run_by_id 里，保证与 CLI 同一条路径。"""
    if body.decision == "MODIFY":
        raise HTTPException(
            status_code=501,
            detail="MODIFY 请走 CLI（会生成新版本，接口层暂未开放）",
        )
    result = graph.run_by_id(
        suggestion_id, body.decision, actor.actor, actor.role, execute=False
    )
    if result.get("errors"):
        code = result["errors"][0]["code"]
        status = {
            "AUTH_FAILED": 403,
            "STATE_CONFLICT": 409,
            "NOT_FOUND": 404,
        }.get(code, 400)
        raise HTTPException(status_code=status, detail=result["errors"][0])
    return result


@app.post("/suggestions/{suggestion_id}/execute")
def execute(suggestion_id: int, actor: ActorContext = Depends(current_actor)):
    try:
        return executor.submit(suggestion_id, actor.actor, actor.role)
    except Exception as e:
        code = getattr(e, "code", "INTERNAL_ERROR")
        status = {
            "NOT_FOUND": 404,
            "STATE_CONFLICT": 409,
            "APPROVAL_INVALIDATED": 409,
        }.get(code, 400)
        raise HTTPException(
            status_code=status, detail={"code": code, "message": str(e)}
        )


@app.post("/jobs/plan")
def plan_period(
    body: PlanRequest, actor: ActorContext = Depends(current_actor)
):
    """批量计划：与 CLI 的 plan-period 共用 graph.run_period 这一份实现。"""
    result = graph.run_period(
        body.period,
        limit=body.limit,
        quota=body.quota,
        force=body.force,
        actor=actor.actor,
        role=actor.role,
        workers=body.workers,
    )
    if result.get("status") == "SKIPPED":
        raise HTTPException(
            status_code=409,
            detail={"code": "PERIOD_CLOSED", "message": result["reason"]},
        )
    return result


@app.get("/metrics/{period}")
def get_metrics(period: str, actor: ActorContext = Depends(current_actor)):
    start, end = pipeline.period_bounds(period)
    result = metrics.recompute(start, end, config.WAREHOUSE_ID)
    rows = repository.list_suggestions(period=period, limit=1000)
    by_status = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    result["suggestions_by_status"] = by_status
    result["suggestion_total"] = len(rows)
    return result


@app.post("/admin/recover")
def recover(actor: ActorContext = Depends(current_actor)):
    if actor.role != "ADMIN" and actor.role != "SUPERVISOR":
        raise HTTPException(
            status_code=403, detail="恢复操作只允许 ADMIN 或 SUPERVISOR"
        )
    return executor.recover()


@app.post("/intake")
def intake_request(
    body: IntakeRequest, actor: ActorContext = Depends(current_actor)
):
    """自然语言入口：把诉求解析成结构化参数（可选直接跑计划）。"""
    result = intake.answer(body.text) if body.run else intake.parse(body.text)
    if not result["ok"]:
        status = {
            "SCHEMA_INVALID": 422,
            "AMBIGUOUS": 409,
            "NOT_FOUND": 404,
            "UPSTREAM_TIMEOUT": 504,
        }.get(result["code"], 400)
        raise HTTPException(status_code=status, detail=result)
    return result
