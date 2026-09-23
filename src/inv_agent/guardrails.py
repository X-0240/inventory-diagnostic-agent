"""写操作护栏：金额上限、供应商账期、库存上限、供应商额度。

护栏不是 AI 的对手，而是写操作的前置约束（契约已定结论 3）。
审批时冻结一份参数快照，执行前复检，参数变了就拒绝执行（契约关键坑 10）。
"""

def check_amount(amount,role_limit):
    if role_limit is None:
        return None
    if amount>role_limit:
        return {"code":"AMOUNT_OVER_ROLE_LIMIT","limit":role_limit,"actual":amount}
    return None

def check_inventory_cap(available,inbound_qty,order_qty,target_qty_value,cap_multiplier):
    """库存上限：下单后的库存位置不能超过目标库存的倍数。"""
    cap=target_qty_value*cap_multiplier
    projected=available+inbound_qty+order_qty
    if projected>cap:
        return {"code":"INVENTORY_OVER_CAP","limit":round(cap,2),"actual":projected}
    return None

def check_credit_limit(open_po_amount,order_amount,credit_limit):
    """供应商额度：0 表示不限。"""
    if not credit_limit or credit_limit<=0:
        return None
    if open_po_amount+order_amount>credit_limit:
        return {"code":"SUPPLIER_CREDIT_EXCEEDED","limit":credit_limit,
                "actual":round(open_po_amount+order_amount,2)}
    return None

def check_payment_terms(supplier):
    """账期必须已配置且非负，否则不允许自动走完流程。"""
    terms=supplier.get("payment_terms_days")
    if terms is None or terms<0:
        return {"code":"PAYMENT_TERMS_INVALID","limit":0,"actual":terms}
    return None

def validate(context,params):
    """执行前完整校验；返回 {ok, violations[]}。context 由调用方拼好。"""
    violations=[]
    for item in (
        check_amount(context["amount"],context.get("role_amount_limit")),
        check_inventory_cap(context["available"],context["inbound_due"],context["qty"],
                            context["target_qty"],params["inventory_cap_multiplier"]),
        check_credit_limit(context["open_po_amount"],context["amount"],
                           context["supplier"].get("credit_limit",0)),
        check_payment_terms(context["supplier"]),
    ):
        if item:
            violations.append(item)
    return {"ok":not violations,"violations":violations}

def freeze_snapshot(params,supplier):
    """审批时冻结的参数快照：执行前用它复检，保证审批依据没过期。"""
    return {
        "rule_version":params["rule_version"],
        "inventory_cap_multiplier":params["inventory_cap_multiplier"],
        "approver_amount_limit":params["approver_amount_limit"],
        "payment_terms_days":supplier.get("payment_terms_days"),
        "credit_limit":supplier.get("credit_limit"),
    }

def reverify(snapshot,params,supplier):
    """执行前复检：任何关键参数变化都要拒执行。"""
    changes=[]
    if snapshot.get("rule_version")!=params["rule_version"]:
        changes.append({"code":"RULE_VERSION_CHANGED","limit":snapshot.get("rule_version"),
                        "actual":params["rule_version"]})
    if snapshot.get("inventory_cap_multiplier")!=params["inventory_cap_multiplier"]:
        changes.append({"code":"INVENTORY_CAP_CHANGED","limit":snapshot.get("inventory_cap_multiplier"),
                        "actual":params["inventory_cap_multiplier"]})
    if snapshot.get("payment_terms_days")!=supplier.get("payment_terms_days"):
        changes.append({"code":"PAYMENT_TERMS_CHANGED","limit":snapshot.get("payment_terms_days"),
                        "actual":supplier.get("payment_terms_days")})
    return changes
