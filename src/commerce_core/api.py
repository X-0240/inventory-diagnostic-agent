"""载体 HTTP 接口：Agent 读事实的唯一入口，外加一个收货确认写接口。

错误语义（与 Agent 侧错误码对齐）：
  401 → AUTH_FAILED（token 不对）
  404 → NOT_FOUND（商品不存在）
  422 → SCHEMA_INVALID（参数缺失或格式错）
  5xx → INTERNAL_ERROR（载体内部错误）
超时不由这里返回，由调用方按连接超时判成 UPSTREAM_TIMEOUT。
"""

from datetime import date, datetime

from fastapi import (
    Body,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Response,
)
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from commerce_core import config, repository

app = FastAPI(title="commerce-core", version="1.0.0")

# 一次批量最多取多少个 SKU：真实平台接口都有上限，超了让调用方分批
MAX_BATCH_SKUS = 200


def _parse_dt(value):
    """接受 YYYY-MM-DD / YYYY-MM-DD HH:MM:SS / ISO 带 T 三种写法。"""
    text = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise HTTPException(
        status_code=422,
        detail={
            "code": "SCHEMA_INVALID",
            "message": "时间格式应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS",
        },
    )


def require_token(x_api_token: str = Header(default="")):
    """接口鉴权：Agent 侧必须带 X-Api-Token。"""
    if x_api_token != config.API_TOKEN:
        raise HTTPException(
            status_code=401,
            detail={"code": "AUTH_FAILED", "message": "token 无效"},
        )
    return True


def _product_or_404(sku_code):
    product = repository.get_product(sku_code)
    if not product:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "商品不存在: " + sku_code},
        )
    return product


@app.get("/health")
def health():
    return {"status": "ok", "service": "commerce-core", "db": config.DB_NAME}


@app.get("/products")
def products(limit: int = Query(default=0, ge=0), _=Depends(require_token)):
    return {"items": jsonable_encoder(repository.list_products(limit or None))}


@app.get("/products/{sku_code}")
def product(sku_code: str, _=Depends(require_token)):
    return jsonable_encoder(_product_or_404(sku_code))


@app.get("/products/{sku_code}/inventory")
def inventory(
    sku_code: str,
    warehouse_id: str = Query(default="WH1"),
    _=Depends(require_token),
):
    _product_or_404(sku_code)
    row = repository.latest_inventory(sku_code, warehouse_id)
    if not row:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "NOT_FOUND",
                "message": "缺少库存快照: " + sku_code,
            },
        )
    return jsonable_encoder(row)


@app.get("/products/{sku_code}/inbound")
def inbound(
    sku_code: str, until: str = Query(default=""), _=Depends(require_token)
):
    _product_or_404(sku_code)
    until_date = None
    if until:
        try:
            until_date = datetime.strptime(until, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "SCHEMA_INVALID",
                    "message": "until 需要 YYYY-MM-DD",
                },
            ) from None
    rows = repository.list_inbound(sku_code, until_date)
    due = (
        repository.inbound_due_qty(sku_code, until_date)
        if until_date
        else sum(int(r["qty"]) for r in rows)
    )
    return {
        "sku_code": sku_code,
        "due_qty": due,
        "items": jsonable_encoder(rows),
    }


@app.get("/products/{sku_code}/sales")
def sales(
    sku_code: str,
    end: str = Query(default=""),
    days: int = Query(default=56, ge=1, le=730),
    _=Depends(require_token),
):
    _product_or_404(sku_code)
    end_date = date.today()
    if end:
        try:
            end_date = datetime.strptime(end, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "SCHEMA_INVALID",
                    "message": "end 需要 YYYY-MM-DD",
                },
            ) from None
    rows = repository.sales_series(sku_code, end_date, days)
    return {"sku_code": sku_code, "days": days, "items": jsonable_encoder(rows)}


@app.get("/products/{sku_code}/leadtime-bias")
def leadtime_bias(
    sku_code: str,
    limit: int = Query(default=10, ge=1, le=50),
    _=Depends(require_token),
):
    _product_or_404(sku_code)
    return {
        "sku_code": sku_code,
        "items": repository.leadtime_bias(sku_code, limit),
    }


@app.post("/receipts")
def receipts(payload: dict = Body(...), _=Depends(require_token)):
    """收货确认：入参 {sku_code,po_no,qty,occurred_at,idempotency_key}。"""
    fields = ["sku_code", "po_no", "qty", "occurred_at", "idempotency_key"]
    missing = [f for f in fields if payload.get(f) in (None, "")]
    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "SCHEMA_INVALID",
                "message": "缺少字段: " + ",".join(missing),
            },
        )
    try:
        result = repository.confirm_receipt(
            payload["sku_code"],
            payload["po_no"],
            int(payload["qty"]),
            payload["occurred_at"],
            payload["idempotency_key"],
        )
    except ValueError as e:
        raise HTTPException(
            status_code=409, detail={"code": str(e), "message": "收货被拒绝"}
        ) from e
    return jsonable_encoder(result)


@app.get("/facts")
def facts(
    skus: str = Query(default=""),
    warehouse_id: str = Query(default="WH1"),
    end: str = Query(default=""),
    days: int = Query(default=56, ge=1, le=730),
    updated_since: str = Query(default=""),
    if_none_match: str = Header(default=""),
    _=Depends(require_token),
):
    """批量事实接口：一次传 SKU 列表，返回商品/库存/在途/销量/交期偏差 + 版本号。

    支持两种"数据新旧"的判断：
    - If-None-Match：整批指纹没变就回 304，Agent 可以直接复用本地缓存；
    - updated_since：只要该 SKU 任一事实在那之后变过才返回。
    """
    codes = [c.strip() for c in skus.split(",") if c.strip()]
    if not codes:
        raise HTTPException(
            status_code=422,
            detail={"code": "SCHEMA_INVALID", "message": "skus 不能为空"},
        )
    if len(codes) > MAX_BATCH_SKUS:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "SCHEMA_INVALID",
                "message": "一次最多 %d 个 SKU，请分批" % MAX_BATCH_SKUS,
            },
        )
    end_date = _parse_dt(end).date() if end else date.today()
    data = repository.facts_batch(codes, warehouse_id, end_date, days)
    items = []
    skipped = []
    for code in codes:
        item = data.get(code)
        if not item or (item["product"] is None and item["inventory"] is None):
            # 载体没有这个 SKU（或没有该仓库存）：如实报告，不塞空对象
            skipped.append(code)
            continue
        item["versions"] = repository.versions_of(item)
        item["updated_at"] = repository.updated_at_of(item)
        items.append(item)
    etag = repository.batch_etag(items)
    if if_none_match and if_none_match.strip() == etag:
        # 整批没变：回 304，不带 body
        return Response(status_code=304, headers={"ETag": etag})
    unchanged = []
    if updated_since:
        since = _parse_dt(updated_since)
        keep = []
        for item in items:
            stamp = item.get("updated_at")
            changed = stamp is None or _parse_dt(stamp) > since
            (keep if changed else unchanged).append(item)
        items = keep
    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "warehouse_id": warehouse_id,
        "end": str(end_date),
        "days": int(days),
        "etag": etag,
        "items": jsonable_encoder(items),
        "skipped": skipped,
        "unchanged": jsonable_encoder(unchanged),
    }
    return JSONResponse(content=payload, headers={"ETag": etag})
