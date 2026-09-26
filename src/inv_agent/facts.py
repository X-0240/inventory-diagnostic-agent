"""事实源适配器：把「事实从哪来」收在一个开关后面（config.OMS_MODE）。

table：直读本地库（默认，演示与单元测试用，与 v1.4 行为完全一致）
http ：调载体服务 commerce-core（一期边界：Agent 只读上游，拿不到上游库账号）

两种模式返回同一份行结构，所以切换数据源不改业务逻辑——这也是集成测试的断言点。
"""
from datetime import datetime

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

    def __init__(self,base_url=None,token=None,timeout=None,transport=None):
        self.base_url=(base_url or config.COMMERCE_BASE_URL).rstrip("/")
        self.token=token or config.COMMERCE_API_TOKEN
        self.timeout=float(timeout or config.COMMERCE_TIMEOUT_SECONDS)
        self.transport=transport
        self._products={}
        self._client=None

    def _http(self):
        #连接复用：HTTP 建连在批量下同样是瓶颈，实测过每请求新建连接会拖慢一个量级
        #trust_env=False：内网服务调用不能走系统代理
        if self._client is None:
            self._client=httpx.Client(timeout=self.timeout,trust_env=False,
                                      transport=self.transport)
        return self._client

    def _get(self,path,params=None):
        try:
            r=self._http().get(self.base_url+path,params=params or {},
                               headers={"X-Api-Token":self.token})
        except httpx.HTTPError as e:
            #超时/连不上：这不是"数据没有"，是"上游不可用"，两者处置不同
            raise PlanError("UPSTREAM_TIMEOUT","载体不可用: "+type(e).__name__)
        if r.status_code==401:
            raise PlanError("AUTH_FAILED","载体拒绝鉴权")
        if r.status_code==404:
            raise PlanError("NOT_FOUND","载体查不到资源: "+path)
        if r.status_code>=400:
            raise PlanError("INTERNAL_ERROR","载体返回 %d: %s"%(r.status_code,r.text[:120]))
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
        p=self._params_for(sku["sku_code"])
        return {"id":sku["supplier_id"],"supplier_code":p["supplier_code"],
                "name":p["supplier_name"],"lead_time_days":p["lead_time_days"],
                "lead_time_sigma_days":p["lead_time_sigma_days"],
                "payment_terms_days":p["payment_terms_days"],
                "credit_limit":p["credit_limit"],"status":"ACTIVE"}

    def get_inventory(self,sku,warehouse_id):
        row=self._get("/products/"+sku["sku_code"]+"/inventory",{"warehouse_id":warehouse_id})
        row["sku_code"]=row.get("sku_code",sku["sku_code"])
        row["snapshot_date"]=_as_date(row.get("snapshot_date"))
        return row

    def get_sales_series(self,sku,end_date,days):
        data=self._get("/products/"+sku["sku_code"]+"/sales",
                       {"end":str(end_date),"days":int(days)})
        out=[]
        for r in data["items"]:
            out.append({"sale_date":_as_date(r["sale_date"]),"qty":int(r["qty"])})
        return out

    def get_inbound_due(self,sku,until_date):
        data=self._get("/products/"+sku["sku_code"]+"/inbound",{"until":str(until_date)})
        return int(data["due_qty"])

    def get_leadtime_bias(self,sku,limit=10):
        data=self._get("/products/"+sku["sku_code"]+"/leadtime-bias",{"limit":int(limit)})
        return [int(x) for x in data["items"]]

    def get_unit_price(self,sku):
        #单价也以上游为准；上游调价必须能被执行前复检抓到
        return float(self._params_for(sku["sku_code"])["unit_cost"])

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
