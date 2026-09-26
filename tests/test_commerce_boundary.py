"""载体边界测试：换数据源不改结论 + 上游不可用的处置 + 账号隔离 + 收货幂等。

这些用例要连载体服务；服务没起时夹具会自己拉起来，起不来才 skip。
"""
import os

import httpx
import pytest

class _DeadTransport(httpx.BaseTransport):
    """固定抛连接错误：不依赖本机某个端口是否真的没人监听。"""
    def handle_request(self,request):
        raise httpx.ConnectError("connection refused",request=request)

def dead_source():
    from inv_agent import facts
    return facts.HttpFactsSource(base_url="http://127.0.0.1:8001",transport=_DeadTransport())

def test_table_and_http_give_same_plan(commerce_url,db_ready):
    """同一批 SKU、同一周期：只换事实源，数量/金额/内容哈希必须一致。"""
    from inv_agent import facts, pipeline, repository
    period="2011-W30"
    table=facts.TableFactsSource()
    http=facts.HttpFactsSource(base_url=commerce_url)
    checked=0
    for sku in repository.active_skus(limit=20):
        try:
            facts.set_source(table)
            a=pipeline.evaluate(sku,period)
            facts.set_source(http)
            b=pipeline.evaluate(sku,period)
        finally:
            facts.set_source(None)
        assert (a["qty"],a["amount"],a["content_hash"])==(b["qty"],b["amount"],b["content_hash"]), \
            "换数据源改变了结论: "+sku["sku_code"]
        checked+=1
    assert checked>=10

def test_upstream_down_is_upstream_timeout(commerce_url,db_ready):
    """上游连不上是 UPSTREAM_TIMEOUT，不是 NOT_FOUND——两者的处置完全不同。"""
    from inv_agent import facts, pipeline, repository
    sku=repository.active_skus(limit=1)[0]
    facts.set_source(dead_source())
    try:
        with pytest.raises(pipeline.PlanError) as e:
            pipeline.evaluate(sku,"2011-W30")
        assert e.value.code=="UPSTREAM_TIMEOUT"
    finally:
        facts.set_source(None)

def test_batch_fails_loudly_when_carrier_down(commerce_url,db_ready):
    """载体的商品清单都拉不到时：任务判 FAILED 并写审计，不能报成功或留悬锁。"""
    from inv_agent import db, facts, graph
    period="2011-W47"
    facts.set_source(dead_source())
    try:
        result=graph.run_period(period,limit=3,force=True)
    finally:
        facts.set_source(None)
    assert result["status"]=="FAILED"
    assert result["stats"]["error_code"]=="UPSTREAM_TIMEOUT"
    row=db.query_one("SELECT status,error_code FROM job_run WHERE job_name='plan_period' "
                     "AND period=%s",(period,))
    assert row["status"]=="FAILED" and row["error_code"]=="UPSTREAM_TIMEOUT"
    hit=db.query_one("SELECT COUNT(*) AS n FROM audit_event WHERE event_type='PERIOD_PLAN_FAILED' "
                     "AND payload_json LIKE %s",("%UPSTREAM_TIMEOUT%",))
    assert int(hit["n"])>0,"失败必须留审计"
    #清掉这次测试插入的任务行，避免影响后续重跑（审计表不可删，按契约保留痕迹）
    with db.tx() as cur:
        cur.execute("DELETE FROM job_run WHERE job_name='plan_period' AND period=%s",(period,))

def test_agent_account_cannot_read_carrier_db(db_ready):
    """两条边界不是口头的：Agent 的库账号对载体库没有权限。"""
    import pymysql
    from inv_agent import db
    with pytest.raises(pymysql.err.MySQLError):
        db.query_one("SELECT COUNT(*) AS n FROM commerce.product")

def test_receipt_is_idempotent_and_bounded(commerce_url):
    """收货确认：幂等键命中返回原流水、不重复入库；超量收货被拒。"""
    from commerce_core import db as cdb
    token=os.getenv("COMMERCE_API_TOKEN","local-commerce-token")
    headers={"X-Api-Token":token}
    sku="TEST-RCV-001"
    with cdb.tx() as cur:
        cur.execute("DELETE FROM inbound WHERE sku_code=%s",(sku,))
        cur.execute("DELETE FROM inventory_snapshot WHERE sku_code=%s",(sku,))
        cur.execute("INSERT INTO inventory_snapshot (sku_code,warehouse_id,snapshot_date,"
                    "qty_on_hand,qty_reserved,version) VALUES (%s,'WH-TEST','2026-09-25',100,0,1)",
                    (sku,))
        cur.execute("INSERT INTO inbound (sku_code,warehouse_id,po_no,qty,expected_date,status) "
                    "VALUES (%s,'WH-TEST','PO-RCV-1',40,'2026-09-26','IN_TRANSIT')",(sku,))
    body={"sku_code":sku,"po_no":"PO-RCV-1","qty":40,"occurred_at":"2026-09-26 10:00:00",
          "idempotency_key":"rcv-test-001"}
    try:
        #先测边界：收 41 件 > 在途 40 件，必须拒（否则库存被凭空放大）
        over=dict(body); over["idempotency_key"]="rcv-test-000"; over["qty"]=41
        r_over=httpx.post(commerce_url+"/receipts",json=over,headers=headers,timeout=10)
        assert r_over.status_code==409 and r_over.json()["detail"]["code"]=="QTY_OUT_OF_RANGE"
        first=httpx.post(commerce_url+"/receipts",json=body,headers=headers,timeout=10).json()
        assert first["replayed"] is False
        second=httpx.post(commerce_url+"/receipts",json=body,headers=headers,timeout=10).json()
        assert second["replayed"] is True,"重复提交必须返回原流水"
        inv=cdb.query_one("SELECT qty_on_hand FROM inventory_snapshot WHERE sku_code=%s",(sku,))
        assert int(inv["qty_on_hand"])==140,"库存只能加一次"
        inbound=cdb.query_one("SELECT qty,status FROM inbound WHERE sku_code=%s",(sku,))
        assert int(inbound["qty"])==0 and inbound["status"]=="RECEIVED"
    finally:
        with cdb.tx() as cur:
            cur.execute("DELETE FROM receipt WHERE sku_code=%s",(sku,))
            cur.execute("DELETE FROM inbound WHERE sku_code=%s",(sku,))
            cur.execute("DELETE FROM inventory_snapshot WHERE sku_code=%s",(sku,))
