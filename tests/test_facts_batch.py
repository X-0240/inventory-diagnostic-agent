"""批量事实接口测试：换批量不改结论、N+1 压成 1 次、ETag/304、版本可判断新旧。

这一版只加两件事：批量取数 + 版本字段。没有引入任何新状态机。
"""
import httpx
import pytest

from inv_agent import config, facts, pipeline, repository

def _http_source(url):
    return facts.HttpFactsSource(base_url=url)

def test_batch_gives_same_plan_as_per_sku(commerce_url,db_ready):
    """同一批 SKU：逐 SKU 取数 vs 批量取数，数量/金额/内容哈希必须完全一致。"""
    period="2011-W30"
    skus=repository.active_skus(limit=20)
    per_sku=_http_source(commerce_url)
    batched=_http_source(commerce_url)
    batched.prefetch(skus,pipeline.period_bounds(period)[0],config.WAREHOUSE_ID)
    checked=0
    for sku in skus:
        try:
            facts.set_source(per_sku)
            a=pipeline.evaluate(sku,period)
            facts.set_source(batched)
            b=pipeline.evaluate(sku,period)
        except pipeline.PlanError:
            continue
        finally:
            facts.set_source(None)
        assert (a["qty"],a["amount"],a["content_hash"])==(b["qty"],b["amount"],b["content_hash"]), \
            "批量取数改变了结论: "+sku["sku_code"]
        checked+=1
    assert checked>=10

def test_batch_turns_n_plus_one_into_one_call(commerce_url,db_ready):
    """20 个 SKU：逐 SKU 要几十次调用；批量模式只打 1 次 /facts。"""
    period="2011-W30"
    skus=repository.active_skus(limit=20)
    start=pipeline.period_bounds(period)[0]
    per_sku=_http_source(commerce_url)
    for sku in skus:
        facts.set_source(per_sku)
        try:
            pipeline.evaluate(sku,period)
        except pipeline.PlanError:
            pass
        finally:
            facts.set_source(None)
    lonely=per_sku.fetch_stats
    assert lonely["per_sku_calls"]>=4*len(skus),"逐 SKU 模式本来就该是 N+1"
    batched=_http_source(commerce_url)
    batched.prefetch(skus,start,config.WAREHOUSE_ID)
    for sku in skus:
        facts.set_source(batched)
        try:
            pipeline.evaluate(sku,period)
        except pipeline.PlanError:
            pass
        finally:
            facts.set_source(None)
    stats=batched.fetch_stats
    assert stats["batch_calls"]==1,"整批只该打一次批量接口"
    assert stats["per_sku_calls"]==0,"命中批量缓存后不该再逐 SKU 取数"
    assert stats["http_calls"]==1

def test_etag_304_reuses_cache(commerce_url,db_ready):
    """TTL 过期后再拉：带上 If-None-Match，上游回 304，Agent 继续用缓存。"""
    from datetime import timedelta
    period="2011-W30"
    skus=repository.active_skus(limit=10)
    start=pipeline.period_bounds(period)[0]
    src=_http_source(commerce_url)
    src.prefetch(skus,start,config.WAREHOUSE_ID)
    first_versions=dict(src._batch["items"][skus[0]["sku_code"]]["versions"])
    src._batch["fetched_at"]=src._batch["fetched_at"]-config.COMMERCE_CACHE_TTL_SECONDS-1
    src.prefetch(skus,start,config.WAREHOUSE_ID)
    assert src.fetch_stats["not_modified"]==1,"没变就该是 304"
    assert src.fetch_stats["batch_calls"]==2,"过期后仍要问上游一次"
    assert src._batch["items"][skus[0]["sku_code"]]["versions"]==first_versions,"304 之后缓存必须还在"

def test_versions_present_and_change_when_upstream_writes(commerce_url):
    """商品/库存/在途都带版本；上游收货后库存版本与整批 ETag 必须变化。"""
    from commerce_core import db as cdb
    token=config.COMMERCE_API_TOKEN
    headers={"X-Api-Token":token}
    sku="TEST-BATCH-001"
    with cdb.tx() as cur:
        cur.execute("DELETE FROM inbound WHERE sku_code=%s",(sku,))
        cur.execute("DELETE FROM inventory_snapshot WHERE sku_code=%s",(sku,))
        cur.execute("INSERT INTO inventory_snapshot (sku_code,warehouse_id,snapshot_date,"
                    "qty_on_hand,qty_reserved,version) VALUES (%s,'WH-TEST','2026-09-25',10,0,1)",(sku,))
        cur.execute("INSERT INTO inbound (sku_code,warehouse_id,po_no,qty,expected_date,status) "
                    "VALUES (%s,'WH-TEST','PO-B1',5,'2026-09-26','IN_TRANSIT')",(sku,))
    url=commerce_url+"/facts?skus="+sku+"&warehouse_id=WH-TEST&end=2026-09-26&days=7"
    try:
        first=httpx.get(url,headers=headers,timeout=10).json()
        item=first["items"][0]
        assert item["versions"]["inventory"]==1
        assert item["updated_at"],"要能看出这份事实是什么时候更新的"
        receipt={"sku_code":sku,"po_no":"PO-B1","qty":5,"occurred_at":"2026-09-26 10:00:00",
                 "idempotency_key":"batch-test-001"}
        assert httpx.post(commerce_url+"/receipts",json=receipt,headers=headers,timeout=10).status_code==200
        second=httpx.get(url,headers=headers,timeout=10).json()
        item2=second["items"][0]
        assert item2["versions"]["inventory"]>item["versions"]["inventory"],"上游收货后库存版本必须抬"
        assert item2["versions"]["inbound"]>item["versions"]["inbound"],"在途版本也要抬"
        assert second["etag"]!=first["etag"],"事实变了整批指纹必须变"
    finally:
        with cdb.tx() as cur:
            cur.execute("DELETE FROM receipt WHERE sku_code=%s",(sku,))
            cur.execute("DELETE FROM inbound WHERE sku_code=%s",(sku,))
            cur.execute("DELETE FROM inventory_snapshot WHERE sku_code=%s",(sku,))

def test_updated_since_filters_by_freshness(commerce_url,db_ready):
    """updated_since 只回在那之后变过的 SKU；没变的进 unchanged。"""
    headers={"X-Api-Token":config.COMMERCE_API_TOKEN}
    codes=",".join(r["sku_code"] for r in repository.active_skus(limit=5))
    base=commerce_url+"/facts?skus="+codes+"&end=2011-05-15&days=7"
    all_items=httpx.get(base+'&updated_since=1990-01-01',headers=headers,timeout=20).json()
    assert len(all_items["items"])==5 and all_items["unchanged"]==[]
    none_changed=httpx.get(base+'&updated_since=2099-01-01',headers=headers,timeout=20).json()
    assert none_changed["items"]==[] and len(none_changed["unchanged"])==5
    assert none_changed["etag"]==all_items["etag"],"过滤不该改变整批指纹"

def test_batch_rejects_too_many_skus(commerce_url):
    """批量接口有上限：超了要明确报错，让调用方分批，而不是悄悄截断。"""
    headers={"X-Api-Token":config.COMMERCE_API_TOKEN}
    codes=",".join("SKU%04d"%i for i in range(201))
    r=httpx.get(commerce_url+"/facts?skus="+codes,headers=headers,timeout=20)
    assert r.status_code==422
    assert r.json()["detail"]["code"]=="SCHEMA_INVALID"
