"""LangGraph 编排：一个 SKU 一个周期一条流程，审批节点用 interrupt 暂停等人。

契约 10.7/短审：MySQL 的业务状态是权威源，checkpoint 只存轨迹与续跑位置。
批量路径（mode='plan'）跑同一条图但不进入 interrupt；单 SKU 路径（mode='approve'）
会停在审批节点，等人给决定后再 resume。
"""
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from inv_agent import (config, diagnosis, executor, guardrails, llm, metrics, pipeline,
                       repository)

class RunState(TypedDict, total=False):
    sku_code: str
    period: str
    mode: str
    diagnose: bool
    actor: str
    role: str
    decision: str
    plan: dict
    diagnosis: dict          #必须声明：LangGraph 只跟踪声明过的状态键，漏声明会被静默丢弃
    case_id: Optional[int]
    suggestion_id: Optional[int]
    guardrails: dict
    approval: dict
    execution: dict
    recompute: dict
    errors: list

def _checkpointer():
    """优先用 SQLite 持久化 checkpoint（支持跨进程续跑），失败时退化为内存。"""
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
        config.DATA_DIR.mkdir(parents=True,exist_ok=True)
        conn=sqlite3.connect(str(config.DATA_DIR/"checkpoints.sqlite"),check_same_thread=False)
        return SqliteSaver(conn)
    except Exception:
        from langgraph.checkpoint.memory import InMemorySaver
        return InMemorySaver()

def n_load(state):
    """读事实并做纯评估；这里不写库。"""
    sku=repository.db.query_one("SELECT * FROM sku WHERE sku_code=%s",(state["sku_code"],))
    if not sku:
        return {"errors":[{"code":"NOT_FOUND","message":"SKU 不存在"}]}
    try:
        plan=pipeline.evaluate(sku,state["period"])
    except pipeline.PlanError as e:
        return {"errors":[{"code":e.code,"message":str(e)}]}
    return {"plan":plan}

def route_after_load(state):
    if state.get("errors"):
        return "finish"
    if not state.get("plan") or state["plan"]["qty"]<=0:
        #不需要补货：不生成建议单，只留一条审计（否则会堆一堆 qty=0 的垃圾建议）
        return "skip"
    return "diagnose" if state.get("diagnose") else "persist"

def n_skip(state):
    plan=state.get("plan") or {}
    sku=plan.get("sku") or {}
    repository.audit("sku",sku.get("id",0),"NO_ACTION","system","SYSTEM",
                     {"period":state.get("period"),"inventory_position":plan.get("facts",{}).get("inventory_position"),
                      "reorder_point":plan.get("facts",{}).get("reorder_point")})
    return {}

def n_diagnose(state):
    """异常归因：调 LLM（或桩）→ schema 校验 → 建/更新异常案件。"""
    plan=state["plan"]
    client=llm.get_client()
    signal=diagnosis.build_signal(plan["facts"],plan["score"],plan["score_detail"],plan["evidence"])
    try:
        result=diagnosis.diagnose(client,signal)
    except Exception as e:
        repository.audit("sku",plan["sku"]["id"],"SCHEMA_INVALID","system","SYSTEM",{"error":str(e)})
        return {"errors":[{"code":"SCHEMA_INVALID","message":str(e)}]}
    case_id=repository.open_case(plan["sku"]["id"],plan["period"],result["hypothesis_type"],
                                 float(result["confidence"]))
    repository.audit("exception_case",case_id,"DIAGNOSED","system","SYSTEM",
                     {"hypothesis":result["hypothesis_type"],"score":plan["score"],
                      "engine":result.get("engine","llm"),"needs_human":result["needs_human"]})
    return {"case_id":case_id,"diagnosis":result}

def n_persist(state):
    plan=state["plan"]
    sku=plan["sku"]
    supplier=plan["facts"]["supplier"]
    params=executor.params_from_config()
    context={
        "amount":plan["amount"],"available":plan["facts"]["available"],
        "inbound_due":plan["facts"]["inbound_due"],"qty":plan["qty"],
        "target_qty":plan["facts"]["target_qty"],"supplier":supplier,
        "open_po_amount":repository.open_po_amount(sku["supplier_id"]),
        "role_amount_limit":None,
    }
    result=guardrails.validate(context,params)
    status=pipeline.plan_status_from_guardrails(result)
    if status=="MANUAL_TAKEOVER":
        case_id=state.get("case_id") or repository.open_case(
            sku["id"],plan["period"],"DATA_ANOMALY",1.0,status="MANUAL_TAKEOVER")
        repository.takeover_case(case_id,"guardrail")
    else:
        case_id=state.get("case_id")
    saved=pipeline.persist_plan(plan,diagnosis=state.get("diagnosis"),case_id=case_id,
                                status=status,violations=result["violations"])
    return {"guardrails":result,"suggestion_id":saved["suggestion_id"]}

def n_approval(state):
    """审批节点：批量模式直接结束；单 SKU 模式用 interrupt 等人。"""
    if state.get("mode")!="approve":
        return {}
    suggestion=repository.get_suggestion(state["suggestion_id"])
    if not suggestion or suggestion["status"]!="PENDING_APPROVAL":
        return {"approval":{"decision":"SKIPPED","reason":"状态不是待审批"}}
    payload={
        "suggestion_id":suggestion["id"],
        "sku_code":state["sku_code"],
        "qty":suggestion["qty"],
        "amount":float(suggestion["amount"]),
        "content_hash":suggestion["content_hash"],
        "hypothesis":suggestion["hypothesis_type"],
        "confidence":float(suggestion["confidence"]) if suggestion["confidence"] is not None else None,
    }
    decision=interrupt(payload)
    return {"decision":decision}

def n_record_approval(state):
    decision=state.get("decision") or "REJECT"
    actor=state.get("actor","unknown")
    role=state.get("role","APPROVER")
    suggestion=repository.get_suggestion(state["suggestion_id"])
    if not suggestion:
        return {"errors":[{"code":"NOT_FOUND","message":"建议不存在"}]}
    if role=="APPROVER" and float(suggestion["amount"])>config.APPROVER_AMOUNT_LIMIT:
        repository.audit("suggestion",suggestion["id"],"AUTH_FAILED",actor,role,
                         {"reason":"金额超过审批人权限"})
        return {"errors":[{"code":"AUTH_FAILED","message":"金额超过审批人权限，需主管审批"}]}
    if actor in ("system",""):
        return {"errors":[{"code":"AUTH_FAILED","message":"审批人不能是 system"}]}
    supplier=repository.get_supplier(repository.get_sku(suggestion["sku_id"])["supplier_id"])
    snapshot=guardrails.freeze_snapshot(executor.params_from_config(),supplier)
    repository.record_approval(suggestion["id"],decision,actor,role,
                               suggestion["content_hash"],snapshot,
                               comment="decision="+decision)
    if decision=="APPROVE":
        repository.set_status(suggestion["id"],"PENDING_APPROVAL","APPROVED",suggestion["content_hash"])
    else:
        repository.set_status(suggestion["id"],"PENDING_APPROVAL","REJECTED")
    repository.audit("suggestion",suggestion["id"],"APPROVAL_"+decision,actor,role,
                     {"snapshot":snapshot})
    return {"approval":{"decision":decision,"actor":actor,"role":role}}

def route_after_approval(state):
    if state.get("errors"):
        return "finish"
    return "execute" if state.get("decision")=="APPROVE" else "finish"

def route_after_approval_node(state):
    """批量模式（plan）不进审批记录；只有 approve 模式才继续到记录审批。"""
    if state.get("mode")!="approve":
        return "finish"
    if state.get("errors"):
        return "finish"
    return "record_approval"

def n_execute(state):
    try:
        result=executor.submit(state["suggestion_id"],state.get("actor","system"),state.get("role","APPROVER"))
    except Exception as e:
        code=getattr(e,"code","INTERNAL_ERROR")
        repository.audit("suggestion",state.get("suggestion_id",0),"EXECUTE_FAILED","system","SYSTEM",
                         {"code":code,"message":str(e)})
        return {"errors":[{"code":code,"message":str(e)}]}
    return {"execution":result}

def n_recompute(state):
    start,end=pipeline.period_bounds(state["period"])
    result=metrics.recompute(start,end,config.WAREHOUSE_ID)
    repository.audit("period",0,"METRICS_RECOMPUTED","system","SYSTEM",result)
    return {"recompute":result}

def n_finish(state):
    errors=state.get("errors") or []
    if errors:
        repository.audit("sku",state.get("plan",{}).get("sku",{}).get("id",0),"PLAN_FAILED",
                         "system","SYSTEM",{"errors":errors})
    return {}

def build():
    g=StateGraph(RunState)
    g.add_node("load",n_load)
    g.add_node("skip",n_skip)
    g.add_node("diagnose",n_diagnose)
    g.add_node("persist",n_persist)
    g.add_node("approval",n_approval)
    g.add_node("record_approval",n_record_approval)
    g.add_node("execute",n_execute)
    g.add_node("recompute",n_recompute)
    g.add_node("finish",n_finish)
    g.add_edge(START,"load")
    g.add_conditional_edges("load",route_after_load,
                            {"diagnose":"diagnose","persist":"persist","skip":"skip","finish":"finish"})
    g.add_edge("skip",END)
    g.add_edge("diagnose","persist")
    g.add_edge("persist","approval")
    g.add_conditional_edges("approval",route_after_approval_node,
                            {"record_approval":"record_approval","finish":"finish"})
    g.add_conditional_edges("record_approval",route_after_approval,{"execute":"execute","finish":"finish"})
    g.add_edge("execute","recompute")
    g.add_edge("recompute","finish")
    g.add_edge("finish",END)
    return g.compile(checkpointer=_checkpointer())

def run_period(period,limit=None,quota=None,force=False,actor="system",role="SYSTEM"):
    """批量计划一个周期：CLI 与 HTTP 接口共用这一份实现。

    步骤：抢 job_lock → 逐 SKU 评估 → 按影响金额取配额 → 连续确认后进诊断 →
    逐 SKU 跑图落库 → 记录任务统计与审计。
    """
    from datetime import timedelta

    from inv_agent import anomaly
    job_id=repository.job_begin("plan_period",period,force=force)
    if job_id is None:
        return {"status":"SKIPPED","reason":"该周期已有成功或运行中的任务（job_lock）"}
    start,_=pipeline.period_bounds(period)
    prev=repository.prev_job_stats("plan_period",pipeline.period_of(start-timedelta(days=7)))
    streaks=prev.get("streaks",{}) if isinstance(prev,dict) else {}
    plans=[]
    errors=[]
    for sku in repository.active_skus(limit=limit):
        try:
            plans.append(pipeline.evaluate(sku,period))
        except pipeline.PlanError as e:
            errors.append({"sku":sku["sku_code"],"code":e.code,"message":str(e)})
    candidates=[p for p in plans if pipeline.is_candidate(p)]
    selected,_=anomaly.apply_quota(candidates,quota or config.ANOMALY_DAILY_QUOTA)
    selected_keys={p["sku"]["sku_code"] for p in selected}
    streaks_next={}
    for p in plans:
        code=p["sku"]["sku_code"]
        streaks_next[code]=int(streaks.get(code,0))+1 if p in candidates else 0
    confirmed={c for c,s in streaks_next.items() if s>=config.ANOMALY_CONFIRM_DAYS}
    diagnose_set=selected_keys & confirmed
    created=0
    takeover=0
    no_action=0
    for p in plans:
        code=p["sku"]["sku_code"]
        result=run(code,period,mode="plan",diagnose=code in diagnose_set)
        if result.get("errors"):
            errors.extend([dict(e,sku=code) for e in result["errors"]])
        if result.get("suggestion_id"):
            created+=1
            if result.get("guardrails",{}).get("ok") is False:
                takeover+=1
        else:
            no_action+=1
    stats={"skus":len(plans),"candidates":len(candidates),"quota_selected":len(selected),
           "confirmed_diagnose":len(diagnose_set),"suggestions_created":created,
           "manual_takeover":takeover,"no_action":no_action,"errors":len(errors),
           "streaks":streaks_next,
           "candidate_top":[{"sku":p["sku"]["sku_code"],"amount":p["amount"],"score":p["score"]}
                            for p in candidates[:20]]}
    repository.job_finish(job_id,"SUCCEEDED",stats)
    repository.audit("period",0,"PERIOD_PLANNED",actor,role,stats)
    return {"period":period,"job_id":job_id,"stats":stats,
            "diagnosed":sorted(diagnose_set),"sample_errors":errors[:10]}

GRAPH=None

def run(sku_code,period,mode="plan",diagnose=True,actor="unknown",role="APPROVER"):
    """跑一次图；mode='approve' 时会停在审批节点（返回 __interrupt__）。"""
    global GRAPH
    if GRAPH is None:
        GRAPH=build()
    #批量模式每次用新 thread：旧 checkpoint 可能带着被重灌删掉的 case_id，
    #业务状态才是权威源，批量路径不该从历史 checkpoint 恢复
    if mode=="plan":
        thread_id=sku_code+":"+period+":plan:"+uuid.uuid4().hex[:8]
    else:
        thread_id=sku_code+":"+period+":"+mode
    cfg={"configurable":{"thread_id":thread_id}}
    state={"sku_code":sku_code,"period":period,"mode":mode,"diagnose":diagnose,
           "actor":actor,"role":role,"errors":[]}
    return GRAPH.invoke(state,cfg)

def resume(sku_code,period,decision,actor,role):
    """用上一次的 checkpoint 续跑（审批决定作为 interrupt 的返回值）。"""
    global GRAPH
    if GRAPH is None:
        GRAPH=build()
    thread_id=sku_code+":"+period+":approve"
    cfg={"configurable":{"thread_id":thread_id}}
    return GRAPH.invoke(Command(resume=decision),cfg)

def run_by_id(suggestion_id,decision,actor,role,execute=True):
    """按建议 ID 走审批：直接用业务状态推进，批量审批场景使用。"""
    suggestion=repository.get_suggestion(suggestion_id)
    if not suggestion:
        return {"errors":[{"code":"NOT_FOUND","message":"建议不存在"}]}
    if suggestion["status"]!="PENDING_APPROVAL":
        return {"errors":[{"code":"STATE_CONFLICT","message":"状态不是待审批: "+suggestion["status"]}]}
    if role=="APPROVER" and float(suggestion["amount"])>config.APPROVER_AMOUNT_LIMIT:
        repository.audit("suggestion",suggestion_id,"AUTH_FAILED",actor,role,
                         {"reason":"金额超过审批人权限"})
        return {"errors":[{"code":"AUTH_FAILED","message":"金额超过审批人权限，需主管审批"}]}
    if actor in ("system",""):
        return {"errors":[{"code":"AUTH_FAILED","message":"审批人不能是 system"}]}
    sku=repository.get_sku(suggestion["sku_id"])
    supplier=repository.get_supplier(sku["supplier_id"])
    snapshot=guardrails.freeze_snapshot(executor.params_from_config(),supplier)
    repository.record_approval(suggestion_id,decision,actor,role,
                               suggestion["content_hash"],snapshot,comment="decision="+decision)
    if decision=="APPROVE":
        repository.set_status(suggestion_id,"PENDING_APPROVAL","APPROVED",suggestion["content_hash"])
    else:
        repository.set_status(suggestion_id,"PENDING_APPROVAL","REJECTED")
    repository.audit("suggestion",suggestion_id,"APPROVAL_"+decision,actor,role,{"snapshot":snapshot})
    out={"approval":{"decision":decision,"actor":actor,"role":role}}
    if decision=="APPROVE" and execute:
        try:
            out["execution"]=executor.submit(suggestion_id,actor,role)
        except Exception as e:
            out["errors"]=[{"code":getattr(e,"code","INTERNAL_ERROR"),"message":str(e)}]
    return out
