"""单价绑定测试：审批的是哪个价，落单就必须是哪个价；上游调价必须挡住执行。

背景（审查点名的 bug）：建议金额用事实源单价算，执行器却读本地 sku.unit_cost，
上游一调价就变成"批的是 A 价、落的是 B 价"。
"""
import httpx
import pytest

from inv_agent import executor, facts, guardrails, pipeline, repository

LOCAL_COST=999.00   # 本地表里的单价（故意和审批单价不同，用来证明执行器不再读它）
FROZEN_PRICE=100.00 # 审批快照冻结的单价

class FixedPriceSource(facts.TableFactsSource):
    """表模式 + 固定单价：模拟"事实源当前给出的单价"。"""
    def __init__(self,price):
        self.price=float(price)

    def get_unit_price(self,sku):
        return self.price

def _fake_product(sku_code,unit_cost):
    return {"id":1,"sku_code":sku_code,"name":"载体商品","category":"GENERAL",
            "supplier_code":"SUP01","supplier_name":"载体供应商","unit_cost":unit_cost,
            "price":unit_cost*2,"lead_time_days":7,"lead_time_sigma_days":1.0,
            "payment_terms_days":30,"credit_limit":100000.0,"moq":1,"pack_size":1,
            "service_level":0.95,"status":"ACTIVE"}

@pytest.fixture()
def approved(db_ready):
    """造一条"已审批通过"的建议：快照冻结单价 100，本地表里却是 999。"""
    from inv_agent import db
    code="TESTPRICE-001"
    with db.tx() as cur:
        cur.execute("INSERT INTO supplier (supplier_code,name,lead_time_days,lead_time_sigma_days,"
                    "payment_terms_days,credit_limit) VALUES ('TESTPRICE','单价测试供应商',7,1.0,30,1000000) "
                    "ON DUPLICATE KEY UPDATE name=VALUES(name)")
        cur.execute("SELECT id FROM supplier WHERE supplier_code='TESTPRICE'")
        supplier_id=cur.fetchone()["id"]
        cur.execute("INSERT INTO sku (sku_code,name,category,supplier_id,unit_cost,price,lead_time_days,"
                    "moq,pack_size,service_level) VALUES (%s,'单价测试商品','TEST',%s,%s,1999.00,7,1,1,0.95) "
                    "ON DUPLICATE KEY UPDATE unit_cost=VALUES(unit_cost)",
                    (code,supplier_id,LOCAL_COST))
        cur.execute("SELECT id FROM sku WHERE sku_code=%s",(code,))
        sku_id=cur.fetchone()["id"]
    suggestion_id=repository.insert_suggestion(
        sku_id=sku_id,period="2099-W11",qty=10,amount=1000.00,unit_price=FROZEN_PRICE,
        basis=[{"source":"test","unit_price":FROZEN_PRICE}],rule_trace=[{"rule":"test"}],
        content_hash="b"*64,status="PENDING_APPROVAL")
    suggestion=repository.get_suggestion(suggestion_id)
    snapshot=guardrails.freeze_approval_snapshot(executor.params_from_config(),
                                                repository.get_supplier(supplier_id),
                                                suggestion,sku_code=code)
    repository.record_approval(suggestion_id,"APPROVE","alice","SUPERVISOR","b"*64,snapshot)
    repository.set_status(suggestion_id,"PENDING_APPROVAL","APPROVED","b"*64)
    yield {"sku_id":sku_id,"supplier_id":supplier_id,"suggestion_id":suggestion_id,
           "sku_code":code,"snapshot":snapshot}
    with db.tx() as cur:
        cur.execute("DELETE FROM inbound_order WHERE sku_id=%s",(sku_id,))
        cur.execute("DELETE FROM purchase_order WHERE sku_id=%s",(sku_id,))
        cur.execute("DELETE FROM approval_record WHERE suggestion_id=%s",(suggestion_id,))
        cur.execute("DELETE FROM replenishment_suggestion WHERE sku_id=%s",(sku_id,))
        cur.execute("DELETE FROM exception_case WHERE sku_id=%s",(sku_id,))
        cur.execute("DELETE FROM policy_snapshot WHERE sku_id=%s",(sku_id,))
        cur.execute("DELETE FROM inventory_snapshot WHERE sku_id=%s",(sku_id,))
        cur.execute("DELETE FROM sales_daily WHERE sku_id=%s",(sku_id,))
        cur.execute("DELETE FROM sku WHERE id=%s",(sku_id,))
        cur.execute("DELETE FROM supplier WHERE id=%s",(supplier_id,))

def test_po_uses_frozen_price_not_local_table(approved):
    """采购单的单价与金额必须来自审批快照，即使本地表里的单价完全不同。"""
    from inv_agent import db
    facts.set_source(FixedPriceSource(FROZEN_PRICE))
    try:
        result=executor.submit(approved["suggestion_id"],"alice","SUPERVISOR")
    finally:
        facts.set_source(None)
    assert result.get("status")!="FAILED"
    po=db.query_one("SELECT unit_price,total_amount,qty FROM purchase_order WHERE sku_id=%s",
                    (approved["sku_id"],))
    assert float(po["unit_price"])==FROZEN_PRICE,"采购单不能再读本地 sku.unit_cost"
    assert float(po["total_amount"])==1000.00
    assert int(po["qty"])==10
    local=repository.get_sku(approved["sku_id"])
    assert float(local["unit_cost"])==LOCAL_COST,"本地表单价没被动过，说明上面的值确实来自快照"

def test_price_drift_blocks_execution(approved):
    """事实源当前单价与审批快照不一致 → APPROVAL_INVALIDATED，并且留审计。"""
    from inv_agent import db
    facts.set_source(FixedPriceSource(FROZEN_PRICE+5))
    try:
        with pytest.raises(executor.ExecutionError) as e:
            executor.submit(approved["suggestion_id"],"alice","SUPERVISOR")
        assert e.value.code=="APPROVAL_INVALIDATED"
    finally:
        facts.set_source(None)
    assert db.query_one("SELECT COUNT(*) AS n FROM purchase_order WHERE sku_id=%s",
                        (approved["sku_id"],))["n"]==0,"被挡住的执行不能留下采购单"
    hit=db.query_one("SELECT payload_json FROM audit_event WHERE entity='suggestion' "
                     "AND entity_id=%s AND event_type='GUARDRAIL_REVERIFY_FAILED' "
                     "ORDER BY id DESC LIMIT 1",(approved["suggestion_id"],))
    assert hit is not None,"拒执行必须留审计"
    assert "UNIT_PRICE_CHANGED" in str(hit["payload_json"])

def test_carrier_price_change_blocks_execution(approved):
    """同一个检查在载体模式下也要生效：接口返回的单价变了就挡。"""
    def handler(request):
        return httpx.Response(200,json=_fake_product(approved["sku_code"],FROZEN_PRICE+1))
    carrier=facts.HttpFactsSource(base_url="http://carrier.test",
                                  transport=httpx.MockTransport(handler))
    facts.set_source(carrier)
    try:
        with pytest.raises(executor.ExecutionError) as e:
            executor.submit(approved["suggestion_id"],"alice","SUPERVISOR")
        assert e.value.code=="APPROVAL_INVALIDATED"
    finally:
        facts.set_source(None)

def test_content_hash_binds_unit_price(db_ready):
    """单价变了，内容哈希必须变——否则旧批准会在调价后继续生效。

    载体模式下商品参数来自 list_products 返回的行，所以"上游调价"就是同一行的
    unit_cost 变了；这里直接改行，等价于上游改了价。
    """
    period="2011-W30"
    base=bumped=None
    #找一个当期真有下单量的 SKU：数量为 0 时金额恒为 0，比不出单价的影响
    for sku in repository.active_skus(limit=40):
        try:
            base=pipeline.evaluate(sku,period)
        except pipeline.PlanError:
            continue
        if base["qty"]<=0:
            continue
        bumped_row=dict(sku)
        bumped_row["unit_cost"]=float(sku["unit_cost"])+3
        bumped=pipeline.evaluate(bumped_row,period)
        break
    assert bumped is not None,"没找到当期有下单量的 SKU"
    #注意：EOQ 本身也吃单价，所以调价会同时影响数量和金额——这不是缺陷，是公式如此
    assert bumped["amount"]!=base["amount"]
    assert bumped["content_hash"]!=base["content_hash"],"内容哈希必须绑定单价"
    assert bumped["unit_price"]==base["unit_price"]+3,"计划里要能看到它用的单价"
