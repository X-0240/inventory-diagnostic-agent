"""载体侧数据访问：接口只暴露这里定义的读法与收货写入。"""
from decimal import Decimal

from commerce_core import db

def json_default(obj):
    """Decimal / date 的 JSON 兜底：数据库数值列是 Decimal，直接序列化会报错。"""
    if isinstance(obj,Decimal):
        return float(obj)
    if hasattr(obj,"isoformat"):
        return obj.isoformat()
    raise TypeError("不支持的 JSON 类型: "+type(obj).__name__)

def list_products(limit=None):
    sql=("SELECT sku_code,name,category,supplier_code,supplier_name,unit_cost,price,"
         "lead_time_days,lead_time_sigma_days,payment_terms_days,credit_limit,moq,"
         "pack_size,service_level,status FROM product WHERE status='ACTIVE' ORDER BY sku_code")
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
        cur.execute("UPDATE inbound SET status=IF(qty=%s,'RECEIVED','IN_TRANSIT'), "
                    "actual_date=%s, qty=qty-%s WHERE id=%s",
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
