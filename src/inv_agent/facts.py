"""事实源适配器：把「事实从哪来」收在一个开关后面（config.OMS_MODE）。

table：直读本地库（默认，演示与单元测试用，与 v1.4 行为完全一致）
http ：调载体服务 commerce-core（一期边界：Agent 只读上游，拿不到上游库账号）

两种模式返回同一份行结构，所以切换数据源不改业务逻辑——这也是集成测试的断言点。
"""
from datetime import datetime,timedelta
import time

import httpx

from inv_agent import config, db, repository
from inv_agent.errors import PlanError

def _as_date(value):
    """把接口返回的日期字符串转成 date，保证与表模式的行结构一致。"""
    if value is None or hasattr(value,"year"):
        return value
    return datetime.strptime(str(value)[:10],"%Y-%m-%d").date()

class FactsSource:
    mode="base"
    skipped=[]

    def list_products(self,limit=None):
        raise NotImplementedError

    def get_supplier(self,sku):
        raise NotImplementedError

    def get_inventory(self,sku,warehouse_id):
        raise NotImplementedError

    def get_sales_series(self,sku,end_date,days):
        raise NotImplementedError

    def get_inbound_due(self,sku,until_date):
        raise NotImplementedError

    def get_leadtime_bias(self,sku,limit=10):
        raise NotImplementedError

    def get_unit_price(self,sku):
        """当前采购单价：执行器执行前要拿它和审批快照比，防"批的是A价、落的是B价"。"""
        raise NotImplementedError

    def prefetch(self,products,period_start,warehouse_id):
        """可选：批量预热事实。表模式没有网络往返，这里什么都不做。"""
        return {"http_calls":0}

    def fact_versions(self,sku):
        """该 SKU 事实的版本号（载体模式才有）；表模式返回空，表示"没有版本概念"。"""
        return {}

class TableFactsSource(FactsSource):
    """本地表模式：与 v1.4 的读取路径逐字一致，保证回归不漂移。"""
    mode="table"

    def list_products(self,limit=None):
        return repository.active_skus(limit=limit)

    def get_supplier(self,sku):
        return repository.get_supplier(sku["supplier_id"])

    def get_inventory(self,sku,warehouse_id):
        return repository.latest_inventory(sku["id"],warehouse_id)

    def get_sales_series(self,sku,end_date,days):
        return repository.sales_series(sku["id"],end_date,days)

    def get_inbound_due(self,sku,until_date):
        return repository.inbound_due(sku["id"],until_date)

    def get_leadtime_bias(self,sku,limit=10):
        rows=db.query_all("SELECT DATEDIFF(actual_date,expected_date) AS bias FROM inbound_order "
                          "WHERE sku_id=%s AND status='RECEIVED' AND actual_date IS NOT NULL "
                          "ORDER BY id DESC LIMIT "+str(int(limit)),(sku["id"],))
        return [int(r["bias"]) for r in rows if r["bias"] is not None]

    def get_unit_price(self,sku):
        row=repository.get_sku(sku["id"])
        return float(row["unit_cost"])

class HttpFactsSource(FactsSource):
    """载体模式：只走 HTTP，拿不到上游库的账号；连接按进程复用。"""
    mode="http"
    #批量接口一次最多取多少 SKU（与载体侧上限一致）
    BATCH_LIMIT=200

    def __init__(self,base_url=None,token=None,timeout=None,transport=None):
        self.base_url=(base_url or config.COMMERCE_BASE_URL).rstrip("/")
        self.token=token or config.COMMERCE_API_TOKEN
        self.timeout=float(timeout or config.COMMERCE_TIMEOUT_SECONDS)
        self.transport=transport
        self._products={}
        self._client=None
        self._batch=None
        self.fetch_stats={"http_calls":0,"batch_calls":0,"per_sku_calls":0,
                          "not_modified":0,"cache_hits":0}

    def _http(self):
        #连接复用：HTTP 建连在批量下同样是瓶颈，实测过每请求新建连接会拖慢一个量级
        #trust_env=False：内网服务调用不能走系统代理
        if self._client is None:
            self._client=httpx.Client(timeout=self.timeout,trust_env=False,
                                      transport=self.transport)
        return self._client

    def _get_response(self,path,params=None,headers=None):
        self.fetch_stats["http_calls"]+=1
        merged={"X-Api-Token":self.token}
        if headers:
            merged.update(headers)
        try:
            r=self._http().get(self.base_url+path,params=params or {},headers=merged)
        except httpx.HTTPError as e:
            #超时/连不上：这不是"数据没有"，是"上游不可用"，两者处置不同
            raise PlanError("UPSTREAM_TIMEOUT","载体不可用: "+type(e).__name__)
        if r.status_code==304:
            return r
        if r.status_code==401:
            raise PlanError("AUTH_FAILED","载体拒绝鉴权")
        if r.status_code==404:
            raise PlanError("NOT_FOUND","载体查不到资源: "+path)
        if r.status_code>=400:
            raise PlanError("INTERNAL_ERROR","载体返回 %d: %s"%(r.status_code,r.text[:120]))
        return r

    def _get(self,path,params=None,headers=None):
        self.fetch_stats["per_sku_calls"]+=1
        r=self._get_response(path,params,headers)
        return r.json()

    def _params_for(self,sku_code):
        """取上游商品参数；一次拉取后在进程内缓存，避免每 SKU 重复拉。"""
        if sku_code not in self._products:
            self._products[sku_code]=self._get("/products/"+sku_code)
        return self._products[sku_code]

    def _merge(self,row,product):
        """本地行只提供身份（id/sku_code），参数一律以上游为准。"""
        merged=dict(row)
        for key in ["name","unit_cost","price","lead_time_days","moq","pack_size","service_level"]:
            if product.get(key) is not None:
                merged[key]=product[key]
        return merged

    def list_products(self,limit=None):
        items=self._get("/products",{"limit":int(limit or 0)})["items"]
        local={r["sku_code"]:r for r in repository.active_skus()}
        out=[]
        self.skipped=[]
        for p in items:
            row=local.get(p["sku_code"])
            if not row:
                #载体有、本地没有引用行：写建议会撞外键，只记录不静默丢弃
                self.skipped.append(p["sku_code"])
                continue
            self._products[p["sku_code"]]=p
            out.append(self._merge(row,p))
        return out[:limit] if limit else out

    def get_supplier(self,sku):
        item=self._batch_item(sku["sku_code"])
        p=(item or {}).get("product") or self._params_for(sku["sku_code"])
        return {"id":sku["supplier_id"],"supplier_code":p["supplier_code"],
                "name":p["supplier_name"],"lead_time_days":p["lead_time_days"],
                "lead_time_sigma_days":p["lead_time_sigma_days"],
                "payment_terms_days":p["payment_terms_days"],
                "credit_limit":p["credit_limit"],"status":"ACTIVE"}

    def get_inventory(self,sku,warehouse_id):
        item=self._batch_item(sku["sku_code"])
        if item is not None:
            row=item.get("inventory")
            if row is None:
                return None
            row=dict(row)
            row["snapshot_date"]=_as_date(row.get("snapshot_date"))
            return row
        row=self._get("/products/"+sku["sku_code"]+"/inventory",{"warehouse_id":warehouse_id})
        row["sku_code"]=row.get("sku_code",sku["sku_code"])
        row["snapshot_date"]=_as_date(row.get("snapshot_date"))
        return row

    def get_sales_series(self,sku,end_date,days):
        item=self._batch_item(sku["sku_code"])
        if item is not None:
            rows=[{"sale_date":_as_date(r["sale_date"]),"qty":int(r["qty"])} for r in item["sales"]
                  if _as_date(r["sale_date"])<=end_date]
            return rows[-int(days):]
        data=self._get("/products/"+sku["sku_code"]+"/sales",
                       {"end":str(end_date),"days":int(days)})
        out=[]
        for r in data["items"]:
            out.append({"sale_date":_as_date(r["sale_date"]),"qty":int(r["qty"])})
        return out

    def get_inbound_due(self,sku,until_date):
        item=self._batch_item(sku["sku_code"])
        if item is not None:
            #交期内到货量由调用方按自己的交期过滤：批量接口返回的是在途明细
            return sum(int(r["qty"]) for r in item["inbound"]
                       if _as_date(r["expected_date"])<=until_date)
        data=self._get("/products/"+sku["sku_code"]+"/inbound",{"until":str(until_date)})
        return int(data["due_qty"])

    def get_leadtime_bias(self,sku,limit=10):
        item=self._batch_item(sku["sku_code"])
        if item is not None:
            return [int(x) for x in item["leadtime_bias"][:int(limit)]]
        data=self._get("/products/"+sku["sku_code"]+"/leadtime-bias",{"limit":int(limit)})
        return [int(x) for x in data["items"]]

    def get_unit_price(self,sku):
        #单价也以上游为准；上游调价必须能被执行前复检抓到
        item=self._batch_item(sku["sku_code"])
        if item is not None and item.get("product"):
            return float(item["product"]["unit_cost"])
        return float(self._params_for(sku["sku_code"])["unit_cost"])

    def prefetch(self,products,period_start,warehouse_id,end_date=None,days=None):
        """一次批量取整批 SKU 的事实，把 N+1 压成 1 次调用。

        缓存策略：同一个请求指纹（仓、截止日、天数、SKU 列表）在 TTL 内直接复用；
        过了 TTL 先带 If-None-Match 问一次，上游回 304 就继续用旧数据。
        """
        codes=[p["sku_code"] for p in products]
        if not codes:
            return self.fetch_stats
        end=str(end_date or (period_start-timedelta(days=1)))
        days=int(days or config.BASELINE_WINDOW_DAYS*2)
        cached=self._batch
        same_request=(cached is not None and cached["codes"]==tuple(codes)
                      and cached["warehouse_id"]==warehouse_id
                      and cached["end"]==end and cached["days"]>=days)
        if same_request and time.time()-cached["fetched_at"]<config.COMMERCE_CACHE_TTL_SECONDS:
            self.fetch_stats["cache_hits"]+=1
            return self.fetch_stats
        headers={}
        if same_request and cached.get("etag"):
            headers["If-None-Match"]=cached["etag"]
        params={"skus":",".join(codes),"warehouse_id":warehouse_id,"end":end,"days":days}
        self.fetch_stats["batch_calls"]+=1
        #分批发：真实接口有单次上限，这里也不假装能一次要一千个
        items={}
        etag=None
        for i in range(0,len(codes),self.BATCH_LIMIT):
            page=codes[i:i+self.BATCH_LIMIT]
            page_headers=headers if i==0 else {}
            params["skus"]=",".join(page)
            r=self._get_response("/facts",params,page_headers)
            if r.status_code==304:
                #整批没变：沿用上次的条目
                self.fetch_stats["not_modified"]+=1
                items=dict(cached["items"])
                etag=cached.get("etag")
                continue
            payload=r.json()
            etag=payload.get("etag") or etag
            for item in payload["items"]:
                items[item["sku_code"]]=item
            self._products.update({item["sku_code"]:item["product"]
                                   for item in payload["items"] if item.get("product")})
        self._batch={"codes":tuple(codes),"warehouse_id":warehouse_id,"end":end,
                     "days":days,"items":items,"etag":etag,"fetched_at":time.time()}
        return self.fetch_stats

    def _batch_item(self,sku_code):
        if self._batch and sku_code in self._batch["items"]:
            self.fetch_stats["cache_hits"]+=1
            return self._batch["items"][sku_code]
        return None

    def fact_versions(self,sku):
        item=self._batch_item(sku["sku_code"])
        return item.get("versions",{}) if item else {}

_source=None

def source():
    """进程内单例：批量扫描时不要每个 SKU 都重建客户端。"""
    global _source
    if _source is None:
        _source=HttpFactsSource() if config.OMS_MODE=="http" else TableFactsSource()
    return _source

def set_source(new_source):
    """测试用：显式替换事实源（None 表示下次调用按配置重建）。"""
    global _source
    _source=new_source
    return _source
