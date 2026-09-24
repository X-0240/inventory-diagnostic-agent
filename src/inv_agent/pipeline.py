"""计划层：把一个 SKU 一个周期的事实 → 基线 → 异常 → 建议 → 护栏 串起来。

这是唯一一份评估实现：LangGraph 的节点、批量扫描、单 SKU 运行都调用这里，
避免出现"两条平行实现"（契约 10.9 冻结 + playbook 单一权威实现）。
"""
import hashlib
import json
from datetime import date, timedelta

from inv_agent import anomaly, compute, config, db, guardrails, repository

class PlanError(RuntimeError):
    def __init__(self,code,message):
        super().__init__(message)
        self.code=code

def period_bounds(period):
    """ISO 周字符串（2011-W05）→ (周一, 周日)。"""
    year,week=period.split("-W")
    start=date.fromisocalendar(int(year),int(week),1)
    return start,start+timedelta(days=6)

def period_of(d):
    y,w,_=d.isocalendar()
    return "%04d-W%02d"%(y,w)

def list_periods(limit=None):
    rows=db.query_all("SELECT DISTINCT period FROM policy_snapshot ORDER BY period")
    periods=[r["period"] for r in rows]
    return periods[:limit] if limit else periods

def load_facts(sku,period,warehouse_id):
    """取该 SKU 在该周期开始时点的事实；每类事实都带来源，便于写进 basis。"""
    start,end=period_bounds(period)
    inv=repository.latest_inventory(sku["id"],warehouse_id)
    if not inv:
        raise PlanError("NOT_FOUND","缺少库存快照 sku="+str(sku["id"]))
    if (start-inv["snapshot_date"]).days>7:
        raise PlanError("STALE_DATA","库存快照过期: "+str(inv["snapshot_date"]))
    supplier=repository.get_supplier(sku["supplier_id"])
    #取 8 周历史：近 7 天 / 近 28 天 / 前 28 天，用来区分"短期突增"和"需求水平上移"
    series=repository.sales_series(sku["id"],start-timedelta(days=1),config.BASELINE_WINDOW_DAYS*2)
    qty_series=[int(r["qty"]) for r in series]
    days_with_sales=len([q for q in qty_series[-config.BASELINE_WINDOW_DAYS:] if q>0])
    date_span=config.BASELINE_WINDOW_DAYS
    missing_days=max(0,date_span-len(qty_series))
    baseline=compute.moving_average(qty_series,config.BASELINE_WINDOW_DAYS)
    #v1.1：改用稳健离散度，避免批发型极端订单把安全库存抬高
    sigma=compute.demand_sigma(qty_series,baseline)
    z=compute.z_for_service_level(float(sku["service_level"]))
    safety=compute.safety_stock(z,sigma,int(sku["lead_time_days"]))
    rop=compute.reorder_point(baseline,int(sku["lead_time_days"]),safety)
    target=compute.target_qty(baseline,int(sku["lead_time_days"]),config.REVIEW_PERIOD_DAYS,z,sigma)
    available=int(inv["qty_on_hand"])-int(inv["qty_reserved"])
    due=repository.inbound_due(sku["id"],start+timedelta(days=int(sku["lead_time_days"])))
    recent7=qty_series[-7:]
    recent28=qty_series[-28:]
    prior28=qty_series[-56:-28]
    bias_rows=db.query_all("SELECT DATEDIFF(actual_date,expected_date) AS bias FROM inbound_order "
                           "WHERE sku_id=%s AND status='RECEIVED' AND actual_date IS NOT NULL "
                           "ORDER BY id DESC LIMIT 10",(sku["id"],))
    biases=[int(r["bias"]) for r in bias_rows if r["bias"] is not None]
    return {
        "sku":sku,"period":period,"warehouse_id":warehouse_id,
        "period_start":start,"period_end":end,
        "inventory":inv,"available":available,"inbound_due":due,
        "supplier":supplier,"qty_series":qty_series,"days_with_sales":days_with_sales,
        "missing_days":missing_days,"baseline_daily":round(baseline,3),"sigma":round(sigma,3),
        "z":z,"safety_qty":safety,"reorder_point":rop,"target_qty":target,
        "inventory_position":available+due,
        "recent7_avg":round(sum(recent7)/len(recent7),3) if recent7 else 0.0,
        "recent28_avg":round(sum(recent28)/len(recent28),3) if recent28 else 0.0,
        "prior28_avg":round(sum(prior28)/len(prior28),3) if prior28 else 0.0,
        "zero_days_7":len([q for q in recent7 if q==0]),
        "zero_days_28":len([q for q in recent28 if q==0]),
        "leadtime_bias_days":(sum(biases)/len(biases)) if biases else 0.0,
    }

def evaluate(sku,period,warehouse_id=None,param_snapshot=None):
    """纯评估：不写库。返回计划 dict（含异常分与建议数量）。"""
    warehouse_id=warehouse_id or config.WAREHOUSE_ID
    facts=load_facts(sku,period,warehouse_id)
    recent_days=min(7,len(facts["qty_series"]))
    recent_qty=sum(facts["qty_series"][-recent_days:]) if recent_days else 0
    demand=anomaly.demand_zscore(recent_qty,recent_days,facts["baseline_daily"],facts["sigma"])
    coverage=anomaly.coverage_days(facts["available"],facts["inbound_due"],facts["baseline_daily"])
    #交期偏差用真实到货记录算（expected vs actual），不再用库存倒推
    leadtime_factor=anomaly.leadtime_bias(0,facts["leadtime_bias_days"],
                                          float(facts["supplier"]["lead_time_sigma_days"] or 1.0))
    stockout_factor=anomaly.stockout_factor(coverage)
    zeros_factor=anomaly.zeros_factor(facts["zero_days_7"],facts["zero_days_28"])
    surge_ratio=anomaly.level_ratio(facts["recent7_avg"],facts["recent28_avg"])
    shift_ratio=anomaly.level_ratio(facts["recent28_avg"],facts["prior28_avg"])
    supplier_factor=0.0
    if facts["supplier"]["lead_time_sigma_days"]:
        supplier_factor=min(float(facts["supplier"]["lead_time_sigma_days"])/3.0,1.5)
    flags=0.0
    if facts["days_with_sales"]<7:
        flags+=1.0
    if facts["missing_days"]>0:
        flags+=0.5
    score,detail=anomaly.anomaly_score({
        "demand":demand,
        "coverage":anomaly.coverage_factor(coverage,threshold_days=7.0),
        "leadtime":leadtime_factor,
        "stockout":stockout_factor,
        "zeros":zeros_factor,
        "surge":anomaly.ratio_factor(surge_ratio,1.5),
        "shift":anomaly.ratio_factor(shift_ratio,1.3),
        "supplier":supplier_factor,
        "flags":flags,
    })
    annual_demand=facts["baseline_daily"]*365
    eoq=compute.eoq(annual_demand,config.ORDER_COST,float(sku["unit_cost"]),config.HOLDING_RATE)
    cap_level=int(facts["target_qty"]*config.INVENTORY_CAP_MULTIPLIER)
    qty=compute.suggest_qty(facts["inventory_position"],facts["reorder_point"],facts["target_qty"],
                            int(sku["moq"]),int(sku["pack_size"]),eoq_qty=eoq,cap_level=cap_level)
    amount=compute.impact_amount(qty,float(sku["unit_cost"]))
    basis=[
        {"source":"inventory_snapshot","snapshot_date":str(facts["inventory"]["snapshot_date"]),
         "available":facts["available"],"version":facts["inventory"]["version"]},
        {"source":"sales_daily","window_days":config.BASELINE_WINDOW_DAYS,
         "baseline_daily":facts["baseline_daily"],"sigma":facts["sigma"]},
        {"source":"supplier","lead_time_days":int(sku["lead_time_days"]),
         "payment_terms_days":facts["supplier"]["payment_terms_days"]},
        {"source":"inbound_order","qty_due_in_leadtime":facts["inbound_due"]},
    ]
    rule_trace=[
        {"rule":"safety_stock","z":facts["z"],"sigma":facts["sigma"],
         "lead_time_days":int(sku["lead_time_days"]),"result":facts["safety_qty"]},
        {"rule":"reorder_point","baseline_daily":facts["baseline_daily"],"result":facts["reorder_point"]},
        {"rule":"target_qty","review_days":config.REVIEW_PERIOD_DAYS,"z":facts["z"],
         "sigma":facts["sigma"],"result":facts["target_qty"]},
        {"rule":"eoq","value":round(eoq,2),"order_cost":config.ORDER_COST,"holding_rate":config.HOLDING_RATE},
        {"rule":"inventory_cap","cap_level":cap_level},
    ]
    content_hash=hashlib.sha256(
        ("|".join([str(sku["id"]),period,str(qty),str(amount),config.RULE_VERSION,
                   str(facts["target_qty"]),str(facts["reorder_point"])])).encode("utf-8")
    ).hexdigest()
    return {
        "facts":facts,"sku":sku,"period":period,"warehouse_id":warehouse_id,
        "score":score,"score_detail":detail,"coverage_days":round(coverage,2),
        "surge_ratio":round(surge_ratio,3),"shift_ratio":round(shift_ratio,3),
        "qty":qty,"amount":amount,"content_hash":content_hash,
        "basis":basis,"rule_trace":rule_trace,"eoq":round(eoq,2),
        "evidence":[
            {"tool":"get_inventory","summary":"可用 %d，在途 %d"%(facts["available"],facts["inbound_due"]),
             "snapshot_date":str(facts["inventory"]["snapshot_date"])},
            {"tool":"get_sales_series","summary":"基线 %.2f/天，σ %.2f，%d 天有销量"%(
                facts["baseline_daily"],facts["sigma"],facts["days_with_sales"])},
            {"tool":"compute_policy","summary":"ROP %d，目标 %d"%(facts["reorder_point"],facts["target_qty"])},
        ],
    }

def is_candidate(plan):
    """是否进入异常候选：分数到阈值，且规则路径确实会下单。"""
    return plan["score"]>=config.ANOMALY_ENTER_SCORE and plan["qty"]>0

def persist_plan(plan,diagnosis=None,case_id=None,status="PENDING_APPROVAL",violations=None,actor="system"):
    """落库：策略快照、异常案件、建议、审计。已存在的同内容建议按幂等处理。"""
    facts=plan["facts"]
    sku=plan["sku"]
    repository.upsert_policy_snapshot(sku["id"],plan["period"],{
        "method":"ma%d"%config.BASELINE_WINDOW_DAYS,
        "baseline_daily":facts["baseline_daily"],"sigma":facts["sigma"],
        "sample_days":len(facts["qty_series"]),"service_level":float(sku["service_level"]),
        "z":facts["z"],"safety_qty":facts["safety_qty"],"reorder_point":facts["reorder_point"],
        "target_qty":facts["target_qty"],"rule_version":config.RULE_VERSION,
    })
    existing=repository.active_suggestion(sku["id"],plan["period"])
    if existing:
        #同 SKU 同周期已有活跃建议：内容相同则幂等返回，内容不同则标为被取代
        if existing["content_hash"]==plan["content_hash"]:
            return {"suggestion_id":existing["id"],"replayed":True,"status":existing["status"]}
        repository.supersede_others(sku["id"],plan["period"],existing["id"])
    dup=repository.suggestion_by_hash(sku["id"],plan["period"],plan["content_hash"])
    if dup:
        #非活跃历史版本也是同内容：按幂等返回，避免撞唯一键
        return {"suggestion_id":dup["id"],"replayed":True,"status":dup["status"]}
    hypothesis=None
    evidence=None
    confidence=None
    proposed=None
    conflicting=False
    if diagnosis:
        hypothesis=diagnosis["hypothesis_type"]
        evidence=diagnosis["evidence_refs"]
        confidence=float(diagnosis["confidence"])
        proposed=diagnosis["proposed_actions"]
        conflicting=bool(diagnosis["conflicting_evidence"])
    suggestion_id=repository.insert_suggestion(
        sku_id=sku["id"],period=plan["period"],qty=plan["qty"],amount=plan["amount"],
        basis=plan["basis"],rule_trace=plan["rule_trace"],content_hash=plan["content_hash"],
        case_id=case_id,hypothesis=hypothesis,evidence=evidence,conflicting=conflicting,
        proposed_actions=proposed,confidence=confidence,status=status)
    repository.audit("suggestion",suggestion_id,"SUGGESTION_CREATED",actor,"SYSTEM",
                     {"qty":plan["qty"],"amount":plan["amount"],"score":plan["score"],
                      "coverage_days":plan["coverage_days"],"violations":violations or []})
    return {"suggestion_id":suggestion_id,"replayed":False,"status":status}

def plan_status_from_guardrails(result):
    """护栏不通过 → 不允许进入审批，直接转人工接管。"""
    return "PENDING_APPROVAL" if result["ok"] else "MANUAL_TAKEOVER"
