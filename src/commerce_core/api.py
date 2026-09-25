"""载体 HTTP 接口：Agent 读事实的唯一入口，外加一个收货确认写接口。

错误语义（与 Agent 侧错误码对齐）：
  401 → AUTH_FAILED（token 不对）
  404 → NOT_FOUND（商品不存在）
  422 → SCHEMA_INVALID（参数缺失或格式错）
  5xx → INTERNAL_ERROR（载体内部错误）
超时不由这里返回，由调用方按连接超时判成 UPSTREAM_TIMEOUT。
"""
from datetime import date, datetime

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
from fastapi.encoders import jsonable_encoder

from commerce_core import config, repository

app=FastAPI(title="commerce-core",version="1.0.0")

def require_token(x_api_token: str=Header(default="")):
    """接口鉴权：Agent 侧必须带 X-Api-Token。"""
    if x_api_token!=config.API_TOKEN:
        raise HTTPException(status_code=401,detail={"code":"AUTH_FAILED","message":"token 无效"})
    return True

def _product_or_404(sku_code):
    product=repository.get_product(sku_code)
    if not product:
        raise HTTPException(status_code=404,
                            detail={"code":"NOT_FOUND","message":"商品不存在: "+sku_code})
    return product

@app.get("/health")
def health():
    return {"status":"ok","service":"commerce-core","db":config.DB_NAME}

@app.get("/products")
def products(limit: int=Query(default=0,ge=0),_=Depends(require_token)):
    return {"items":jsonable_encoder(repository.list_products(limit or None))}

@app.get("/products/{sku_code}")
def product(sku_code: str,_=Depends(require_token)):
    return jsonable_encoder(_product_or_404(sku_code))

@app.get("/products/{sku_code}/inventory")
def inventory(sku_code: str,warehouse_id: str=Query(default="WH1"),_=Depends(require_token)):
    _product_or_404(sku_code)
    row=repository.latest_inventory(sku_code,warehouse_id)
    if not row:
        raise HTTPException(status_code=404,
                            detail={"code":"NOT_FOUND","message":"缺少库存快照: "+sku_code})
    return jsonable_encoder(row)

@app.get("/products/{sku_code}/inbound")
def inbound(sku_code: str,until: str=Query(default=""),_=Depends(require_token)):
    _product_or_404(sku_code)
    until_date=None
    if until:
        try:
            until_date=datetime.strptime(until,"%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=422,
                                detail={"code":"SCHEMA_INVALID","message":"until 需要 YYYY-MM-DD"})
    rows=repository.list_inbound(sku_code,until_date)
    due=repository.inbound_due_qty(sku_code,until_date) if until_date else sum(int(r["qty"]) for r in rows)
    return {"sku_code":sku_code,"due_qty":due,"items":jsonable_encoder(rows)}

@app.get("/products/{sku_code}/sales")
def sales(sku_code: str,end: str=Query(default=""),days: int=Query(default=56,ge=1,le=730),
          _=Depends(require_token)):
    _product_or_404(sku_code)
    end_date=date.today()
    if end:
        try:
            end_date=datetime.strptime(end,"%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=422,
                                detail={"code":"SCHEMA_INVALID","message":"end 需要 YYYY-MM-DD"})
    rows=repository.sales_series(sku_code,end_date,days)
    return {"sku_code":sku_code,"days":days,"items":jsonable_encoder(rows)}

@app.get("/products/{sku_code}/leadtime-bias")
def leadtime_bias(sku_code: str,limit: int=Query(default=10,ge=1,le=50),_=Depends(require_token)):
    _product_or_404(sku_code)
    return {"sku_code":sku_code,"items":repository.leadtime_bias(sku_code,limit)}

@app.post("/receipts")
def receipts(payload: dict=Body(...),_=Depends(require_token)):
    """收货确认：入参 {sku_code,po_no,qty,occurred_at,idempotency_key}。"""
    fields=["sku_code","po_no","qty","occurred_at","idempotency_key"]
    missing=[f for f in fields if payload.get(f) in (None,"")]
    if missing:
        raise HTTPException(status_code=422,
                            detail={"code":"SCHEMA_INVALID","message":"缺少字段: "+",".join(missing)})
    try:
        result=repository.confirm_receipt(payload["sku_code"],payload["po_no"],int(payload["qty"]),
                                          payload["occurred_at"],payload["idempotency_key"])
    except ValueError as e:
        raise HTTPException(status_code=409,detail={"code":str(e),"message":"收货被拒绝"})
    return jsonable_encoder(result)
