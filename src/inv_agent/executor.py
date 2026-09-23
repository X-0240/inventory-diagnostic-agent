"""受控执行器：全系统唯一有权写 purchase_order 的模块。

事务边界（契约 10.7 + 短审修正）：
  TX1 条件更新 APPROVED→EXECUTING（带 content_hash 守卫）
  TX2 写采购单（唯一 idempotency_key）—— 一期把这一步当作"外部系统已提交"
  TX3 EXECUTING→EXECUTED
两次事务之间杀进程，会留下"采购单已存在、建议未置 EXECUTED"的窗口，
恢复函数必须识别或回填原单，绝不能新建第二张。
"""
import hashlib
import os

from inv_agent import config,guardrails,repository

class ExecutionError(RuntimeError):
    def __init__(self,code,message):
        super().__init__(message)
        self.code=code

class InjectedFault(RuntimeError):
    """测试用故障注入：模拟 TX2 之后进程被杀。"""

def idempotency_key(sku_id,period,content_hash):
    raw="sku:"+str(sku_id)+"|period:"+period+"|hash:"+content_hash
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:48]

def params_from_config():
    return {
        "rule_version":config.RULE_VERSION,
        "inventory_cap_multiplier":config.INVENTORY_CAP_MULTIPLIER,
        "approver_amount_limit":config.APPROVER_AMOUNT_LIMIT,
    }

def submit(suggestion_id,actor="system",role="APPROVER"):
    """审批通过后执行下单。返回 {po_no,replayed,status}。"""
    suggestion=repository.get_suggestion(suggestion_id)
    if not suggestion:
        raise ExecutionError("NOT_FOUND","建议不存在: "+str(suggestion_id))
    approval=repository.db.query_one(
        "SELECT * FROM approval_record WHERE suggestion_id=%s AND content_hash=%s",
        (suggestion_id,suggestion["content_hash"]))
    if not approval or approval["decision"]!="APPROVE":
        raise ExecutionError("APPROVAL_INVALIDATED","没有与该版本匹配的批准记录")
    sku=repository.get_sku(suggestion["sku_id"])
    supplier=repository.get_supplier(sku["supplier_id"])
    snapshot=approval["approval_snapshot_json"]
    if isinstance(snapshot,str):
        import json
        snapshot=json.loads(snapshot)
    #执行前复检：审批依据里的参数变了就拒执行
    changes=guardrails.reverify(snapshot,params_from_config(),supplier)
    if changes:
        repository.audit("suggestion",suggestion_id,"GUARDRAIL_REVERIFY_FAILED",actor,role,{"changes":changes})
        raise ExecutionError("APPROVAL_INVALIDATED","审批快照与当前参数不一致: "+str(changes))
    #TX1：条件更新，防并发
    rowcount=repository.set_status(suggestion_id,"APPROVED","EXECUTING",suggestion["content_hash"])
    if rowcount==0:
        raise ExecutionError("STATE_CONFLICT","状态已被并发修改，当前非 APPROVED")
    repository.audit("suggestion",suggestion_id,"EXECUTING",actor,role,{"po_intent":True})
    key=idempotency_key(sku["id"],suggestion["period"],suggestion["content_hash"])
    existing=repository.find_po_by_idem(key)
    replayed=False
    if existing:
        #幂等重放：不新建，直接进入回填
        po_no=existing["po_no"]
        replayed=True
    else:
        po_no=repository.next_po_no(suggestion["period"],sku["sku_code"])
        repository.insert_po(sku["supplier_id"],sku["id"],suggestion["qty"],sku["unit_cost"],
                             suggestion["amount"],suggestion_id,key,snapshot,po_no)
        repository.audit("purchase_order",0,"PO_SUBMITTED",actor,role,
                         {"po_no":po_no,"amount":float(suggestion["amount"]),"idempotency_key":key})
    if os.getenv("INV_FAULT_AFTER_PO_INSERT")=="1" and not replayed:
        #故障注入点：采购单已提交，建议还没置 EXECUTED
        raise InjectedFault("注入故障：PO 已写入，建议未置 EXECUTED")
    #TX3：回填建议状态
    rowcount=repository.set_status(suggestion_id,"EXECUTING","EXECUTED")
    if rowcount==0:
        raise ExecutionError("RESULT_UNKNOWN","回填状态失败，需对账")
    repository.audit("suggestion",suggestion_id,"EXECUTED",actor,role,{"po_no":po_no,"replayed":replayed})
    return {"po_no":po_no,"replayed":replayed,"status":"EXECUTED"}

def recover():
    """恢复：把卡在 EXECUTING 的建议按幂等键对账——有原单就回填，没有就判失败。"""
    rows=repository.list_suggestions(status="EXECUTING",limit=500)
    recovered=0
    failed=0
    for s in rows:
        key=idempotency_key(s["sku_id"],s["period"],s["content_hash"])
        po=repository.find_po_by_idem(key)
        if po:
            repository.set_status(s["id"],"EXECUTING","EXECUTED")
            repository.audit("suggestion",s["id"],"RECOVERED_FROM_PO","system","SYSTEM",
                             {"po_no":po["po_no"],"idempotency_key":key})
            recovered+=1
        else:
            repository.set_status(s["id"],"EXECUTING","EXECUTION_FAILED")
            repository.audit("suggestion",s["id"],"EXECUTION_FAILED","system","SYSTEM",
                             {"reason":"无对应采购单，需人工接管"})
            failed+=1
    return {"executing":len(rows),"recovered":recovered,"failed":failed}

def reverse_po(original_po_id,actor="system",role="APPROVER"):
    """反向单：已确认或已产生副作用的采购单只能反向冲销，且必须独立审批与独立幂等键。"""
    original=repository.db.query_one("SELECT * FROM purchase_order WHERE id=%s",(original_po_id,))
    if not original:
        raise ExecutionError("NOT_FOUND","原采购单不存在")
    if original["status"] in ("DRAFT","CANCELLED"):
        raise ExecutionError("RULE_VIOLATION","未提交的采购单应走取消，不应下反向单")
    key=idempotency_key(original["sku_id"],original["po_no"]+":reverse",original["idempotency_key"])
    if repository.find_po_by_idem(key):
        return {"po_no":None,"replayed":True}
    po_no=repository.next_po_no("REV",str(original["po_no"]))
    repository.insert_po(original["supplier_id"],original["sku_id"],-int(original["qty"]),
                         original["unit_price"],-float(original["total_amount"]),None,key,
                         original["approval_snapshot_json"],po_no,reverse_of_po_id=original_po_id)
    repository.audit("purchase_order",original_po_id,"REVERSE_PO_CREATED",actor,role,
                     {"reverse_po_no":po_no,"idempotency_key":key})
    return {"po_no":po_no,"replayed":False}
