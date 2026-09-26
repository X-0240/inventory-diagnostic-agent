"""载体侧数据访问：接口只暴露这里定义的读法与收货写入。"""
from decimal import Decimal
import hashlib
import json

from commerce_core import db

PRODUCT_COLS=("sku_code,name,category,supplier_code,supplier_name,unit_cost,price,"
              "lead_time_days,lead_time_sigma_days,payment_terms_days,credit_limit,moq,"
              "pack_size,service_level,status,data_version")

def json_default(obj):
    """Decimal / date 的 JSON 兜底：数据库数值列是 Decimal，直接序列化会报错。"""
    if isinstance(obj,Decimal):
        return float(obj)
    if hasattr(obj,"isoformat"):
        return obj.isoformat()
    raise TypeError("不支持的 JSON 类型: "+type(obj).__name__)

def list_products(limit=None):
    sql="SELECT "+PRODUCT_COLS+" FROM product WHERE status='ACTIVE' ORDER BY sku_code"
    if limit:
        sql+=" LIMIT "+str(int(limit))
    return db.query_all(sql)

def get_product(sku_code):
    return db.query_one("SELECT * FROM product WHERE sku_code=%s",(sku_code,))

def latest_inventory(sku_code,warehouse_id):
    return db.query_one(
        "SELECT i.id,i.sku_code,i.warehouse_id,i.snapshot_date,i.qty_on_hand,"
        "i.qty_reserved,i.version FROM inventory_snapshot i "
        "WHERE i.sku_code=%s AND i.warehouse_id=%s ORDER BY i.snapshot_date DESC LIMIT 1",
        (sku_code,warehouse_id))

def inbound_due_qty(sku_code,until_date):
    rows=db.query_all(
        "SELECT COALESCE(SUM(qty),0) AS qty FROM inbound "
        "WHERE sku_code=%s AND status='IN_TRANSIT' AND expected_date<=%s",
        (sku_code,until_date))
    return int(rows[0]["qty"]) if rows else 0

def list_inbound(sku_code,until_date=None):
    sql=("SELECT id,sku_code,po_no,qty,expected_date,actual_date,status FROM inbound "
         "WHERE sku_code=%s AND status='IN_TRANSIT'")
    params=[sku_code]
    if until_date:
        sql+=" AND expected_date<=%s"
        params.append(until_date)
    sql+=" ORDER BY expected_date"
    return db.query_all(sql,tuple(params))

def sales_series(sku_code,end_date,days):
    rows=db.query_all(
        "SELECT sale_date,qty FROM sales_daily WHERE sku_code=%s AND sale_date<=%s "
        "ORDER BY sale_date DESC LIMIT "+str(int(days)),(sku_code,end_date))
    return list(reversed(rows))

def leadtime_bias(sku_code,limit=10):
    """已收货在途的实际到货偏差（实际-预计，天）；交期修正的输入。"""
    rows=db.query_all(
        "SELECT DATEDIFF(actual_date,expected_date) AS bias FROM inbound "
        "WHERE sku_code=%s AND status='RECEIVED' AND actual_date IS NOT NULL "
        "ORDER BY id DESC LIMIT "+str(int(limit)),(sku_code,))
    return [int(r["bias"]) for r in rows if r["bias"] is not None]

def confirm_receipt(sku_code,po_no,qty,occurred_at,idempotency_key):
    """收货确认（载体自己的写操作）：写收货流水 + 冲销在途 + 抬库存。

    幂等键命中直接返回原流水，不重复入库；三张表的写入在同一个事务里。
    """
    existing=db.query_one("SELECT * FROM receipt WHERE idempotency_key=%s",(idempotency_key,))
    if existing:
        return {"replayed":True,"receipt":existing}
    with db.tx() as cur:
        row=db.query_one("SELECT * FROM inbound WHERE sku_code=%s AND po_no=%s "
                         "AND status='IN_TRANSIT' ORDER BY id LIMIT 1",(sku_code,po_no))
        if not row:
            raise ValueError("INBOUND_NOT_FOUND")
        #收货量不能超过在途量，否则库存会被凭空放大
        if int(qty)<=0 or int(qty)>int(row["qty"]):
            raise ValueError("QTY_OUT_OF_RANGE")
        wh=row["warehouse_id"]
        #坑：MySQL 的 SET 是左到右赋值，后面的表达式看到的是已改过的值。
        #所以先算 status、再减 qty；顺序反过来会把收完的批次永远留在 IN_TRANSIT。
        #收完在途要同时抬版本：版本不动，Agent 就不知道上游改过事实
        cur.execute("UPDATE inbound SET status=IF(qty=%s,'RECEIVED','IN_TRANSIT'), "
                    "actual_date=%s, qty=qty-%s, data_version=data_version+1 WHERE id=%s",
                    (int(qty),occurred_at,int(qty),row["id"]))
        cur.execute("UPDATE inventory_snapshot SET qty_on_hand=qty_on_hand+%s,version=version+1 "
                    "WHERE sku_code=%s AND warehouse_id=%s AND snapshot_date="
                    "(SELECT d FROM (SELECT MAX(snapshot_date) AS d FROM inventory_snapshot "
                    " WHERE sku_code=%s AND warehouse_id=%s) t)",
                    (int(qty),sku_code,wh,sku_code,wh))
        cur.execute("INSERT INTO receipt (sku_code,po_no,qty,occurred_at,idempotency_key) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (sku_code,po_no,int(qty),occurred_at,idempotency_key))
        receipt_id=cur.lastrowid
    return {"replayed":False,"receipt":{"id":receipt_id,"sku_code":sku_code,"po_no":po_no,
                                        "qty":int(qty),"occurred_at":occurred_at}}

def _in_clause(values):
    return ",".join(["%s"]*len(values))

def facts_batch(sku_codes,warehouse_id,end_date,days):
    """一次取一批 SKU 的事实：接口内部固定用 4 条查询，不按 SKU 循环。

    返回 {sku_code: {...}}；每个事实块都带版本号（product.data_version、
    inventory.version、inbound.data_version），调用方据此判断数据新旧。
    """
    codes=list(dict.fromkeys(sku_codes))
    if not codes:
        return {}
    ph=_in_clause(codes)
    out={c:{"sku_code":c,"product":None,"inventory":None,"inbound":[],
            "sales":[],"leadtime_bias":[]} for c in codes}
    for r in db.query_all("SELECT "+PRODUCT_COLS+" FROM product WHERE sku_code IN ("+ph+")",
                          tuple(codes)):
        out[r["sku_code"]]["product"]=r
    #每个 SKU 的最新一条库存：窗口函数取 rn=1，避免按 SKU 循环
    inv_sql=("SELECT * FROM (SELECT sku_code,warehouse_id,snapshot_date,qty_on_hand,qty_reserved,"
             "version,updated_at,ROW_NUMBER() OVER (PARTITION BY sku_code ORDER BY snapshot_date DESC) rn "
             "FROM inventory_snapshot WHERE sku_code IN ("+ph+") AND warehouse_id=%s) t WHERE rn=1")
    for r in db.query_all(inv_sql,tuple(codes)+ (warehouse_id,)):
        r.pop("rn",None)
        out[r["sku_code"]]["inventory"]=r
    inb_sql=("SELECT sku_code,warehouse_id,po_no,qty,expected_date,actual_date,status,data_version,"
             "updated_at FROM inbound WHERE sku_code IN ("+ph+") AND status='IN_TRANSIT' "
             "ORDER BY sku_code,expected_date")
    for r in db.query_all(inb_sql,tuple(codes)):
        out[r["sku_code"]]["inbound"].append(r)
    #在途版本按 SKU 单独取：收完货的批次会离开在途列表，版本不能因此"消失"
    ver_sql=("SELECT sku_code,MAX(data_version) AS v FROM inbound WHERE sku_code IN ("+ph+") "
             "GROUP BY sku_code")
    for r in db.query_all(ver_sql,tuple(codes)):
        out[r["sku_code"]]["inbound_version"]=int(r["v"])
    sales_sql=("SELECT sku_code,sale_date,qty FROM (SELECT sku_code,sale_date,qty,"
               "ROW_NUMBER() OVER (PARTITION BY sku_code ORDER BY sale_date DESC) rn "
               "FROM sales_daily WHERE sku_code IN ("+ph+") AND sale_date<=%s) t "
               "WHERE rn<=%s ORDER BY sku_code,sale_date")
    for r in db.query_all(sales_sql,tuple(codes)+(end_date,int(days))):
        out[r["sku_code"]]["sales"].append({"sale_date":r["sale_date"],"qty":r["qty"]})
    bias_sql=("SELECT sku_code,bias FROM (SELECT sku_code,DATEDIFF(actual_date,expected_date) AS bias,"
              "ROW_NUMBER() OVER (PARTITION BY sku_code ORDER BY id DESC) rn FROM inbound "
              "WHERE sku_code IN ("+ph+") AND status='RECEIVED' AND actual_date IS NOT NULL) t "
              "WHERE rn<=10 ORDER BY sku_code")
    for r in db.query_all(bias_sql,tuple(codes)):
        out[r["sku_code"]]["leadtime_bias"].append(int(r["bias"]))
    return out

def versions_of(item):
    """一个 SKU 三类事实的版本号；缺哪类就记 0。"""
    product=item.get("product") or {}
    inventory=item.get("inventory") or {}
    inbound=item.get("inbound") or []
    return {"product":int(product.get("data_version") or 0),
            "inventory":int(inventory.get("version") or 0),
            "inbound":int(item.get("inbound_version")
                          or max([int(r.get("data_version") or 0) for r in inbound] or [0]))}

def updated_at_of(item):
    """一个 SKU 的最后变更时间 = 商品/库存/在途三者里最大的那个。"""
    stamps=[]
    for src in [item.get("product"),item.get("inventory")]:
        if src and src.get("updated_at"):
            stamps.append(str(src["updated_at"]))
    for r in item.get("inbound") or []:
        if r.get("updated_at"):
            stamps.append(str(r["updated_at"]))
    return max(stamps) if stamps else None

def batch_etag(items):
    """整批内容的指纹：任一 SKU 的任一版本变了，ETag 必变。"""
    parts=[]
    for item in sorted(items,key=lambda x:x["sku_code"]):
        v=versions_of(item)
        parts.append("%s:%d:%d:%d:%d:%d"%(item["sku_code"],v["product"],v["inventory"],v["inbound"],
                                          len(item.get("sales") or []),
                                          len(item.get("inbound") or [])))
    return '"'+hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]+'"'
