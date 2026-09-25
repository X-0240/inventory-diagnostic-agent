"""把本地生成的事实投影进载体库 commerce（一期载体是本地模拟的上游）。

为什么要有这一步：一期没有真实 ERP/平台可供对接，所以载体是本地起的服务，
数据从同一份模拟结果投影过去，保证两库同源、口径一致。
二期接真上游时这一步消失，只换 COMMERCE_BASE_URL。

顶层直接执行：python scripts/project_to_commerce.py
"""
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))

from inv_agent import config, db
from commerce_core import config as cconfig, db as cdb

PRODUCT_SQL="""
SELECT k.sku_code,k.name,k.category,k.unit_cost,k.price,k.lead_time_days,k.moq,k.pack_size,
       k.service_level,k.status,s.supplier_code,s.name AS supplier_name,s.lead_time_days AS s_lead,
       s.lead_time_sigma_days,s.payment_terms_days,s.credit_limit
FROM sku k JOIN supplier s ON s.id=k.supplier_id
WHERE k.status='ACTIVE'
"""

INV_SQL="""
SELECT k.sku_code,i.warehouse_id,i.snapshot_date,i.qty_on_hand,i.qty_reserved,i.version
FROM inventory_snapshot i JOIN sku k ON k.id=i.sku_id
"""

SALES_SQL="""
SELECT k.sku_code,s.sale_date,s.qty FROM sales_daily s JOIN sku k ON k.id=s.sku_id
"""

INBOUND_SQL="""
SELECT k.sku_code,b.id,b.qty,b.expected_date,b.actual_date,b.status
FROM inbound_order b JOIN sku k ON k.id=b.sku_id
"""

def clear(cur):
    #先清再灌：本地重灌换了 SKU 集合时，载体不残留旧行
    for t in ["receipt","sales_daily","inbound","inventory_snapshot","product"]:
        cur.execute("DELETE FROM "+t)

def load_products(cur):
    rows=db.query_all(PRODUCT_SQL)
    #批量提交：逐行 INSERT 的往返在几万行规模下要几分钟，executemany 只要几秒
    cur.executemany(
        "INSERT INTO product (sku_code,name,category,supplier_code,supplier_name,unit_cost,"
        "price,lead_time_days,lead_time_sigma_days,payment_terms_days,credit_limit,moq,"
        "pack_size,service_level,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        [(r["sku_code"],r["name"],r["category"],r["supplier_code"],r["supplier_name"],
          r["unit_cost"],r["price"],r["s_lead"],r["lead_time_sigma_days"],
          r["payment_terms_days"],r["credit_limit"],r["moq"],r["pack_size"],
          r["service_level"],r["status"]) for r in rows])
    return len(rows)

def load_inventory(cur):
    rows=db.query_all(INV_SQL)
    cur.executemany("INSERT INTO inventory_snapshot (sku_code,warehouse_id,snapshot_date,"
                    "qty_on_hand,qty_reserved,version) VALUES (%s,%s,%s,%s,%s,%s)",
                    [(r["sku_code"],r["warehouse_id"],r["snapshot_date"],r["qty_on_hand"],
                      r["qty_reserved"],r["version"]) for r in rows])
    return len(rows)

def load_sales(cur):
    rows=db.query_all(SALES_SQL)
    cur.executemany("INSERT INTO sales_daily (sku_code,sale_date,qty) VALUES (%s,%s,%s)",
                    [(r["sku_code"],r["sale_date"],r["qty"]) for r in rows])
    return len(rows)

def load_inbound(cur):
    rows=db.query_all(INBOUND_SQL)
    #本地 inbound_order 只有 po_id，投影时给一个稳定的可见单号
    cur.executemany("INSERT INTO inbound (sku_code,warehouse_id,po_no,qty,expected_date,"
                    "actual_date,status) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    [(r["sku_code"],config.WAREHOUSE_ID,
                      "INB-%s-%04d"%(r["sku_code"],int(r["id"])),r["qty"],r["expected_date"],
                      r["actual_date"],r["status"]) for r in rows])
    return len(rows)

def main():
    stats={}
    with cdb.tx() as cur:
        clear(cur)
        stats["product"]=load_products(cur)
        stats["inventory_snapshot"]=load_inventory(cur)
        stats["sales_daily"]=load_sales(cur)
        stats["inbound"]=load_inbound(cur)
    print("投影完成：",stats,"→",cconfig.DB_NAME)

main()
