"""数据管线：公开真实销量（UCI Online Retail，CC BY 4.0）+ 模拟库存/在途/交期/账期。

契约 7（数据来源＝甲）：
- 销量来自公开数据集，仅作需求信号，source_flag='PUBLIC'
- 库存、在途、交期、应付由本模块的模拟器推导，保证自洽（不是拍脑袋生成）
- 参数在对照实验前冻结并写入 config_version 与 data/frozen_params.json（预注册）

用法：python -m inv_agent.data_pipeline --download --build
"""
import argparse
import csv
import hashlib
import json
import math
import random
import subprocess
import sys
import urllib.request
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from inv_agent import config, compute, db, repository

UCI_ZIP="https://archive.ics.uci.edu/static/public/352/online+retail.zip"
XLSX_NAME="Online Retail.xlsx"

SCENARIOS=("NORMAL","PROMOTION_SURGE","SUPPLIER_DELAY","NEW_PRODUCT_NO_HISTORY",
           "STOCKOUT_CASCADE","DEMAND_SHIFT","DATA_ANOMALY")

def download(force=False):
    """下载并解压 UCI 数据集到 data/raw（幂等）。"""
    config.RAW_DIR.mkdir(parents=True,exist_ok=True)
    zip_path=config.RAW_DIR/"online_retail.zip"
    xlsx_path=config.RAW_DIR/XLSX_NAME
    if xlsx_path.exists() and not force:
        return xlsx_path
    if not zip_path.exists() or force:
        with urllib.request.urlopen(UCI_ZIP,timeout=120) as resp, open(zip_path,"wb") as f:
            f.write(resp.read())
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(config.RAW_DIR)
    return xlsx_path

def select_sales(xlsx_path):
    """取 Quantity>0 的行作为需求信号，按 StockCode 汇总日均，选销量最高的前 N 个 SKU。"""
    df=pd.read_excel(xlsx_path)
    df=df.dropna(subset=["StockCode","InvoiceDate"])
    df=df[df["Quantity"]>0]
    df["sale_date"]=pd.to_datetime(df["InvoiceDate"]).dt.date
    start=date.fromisoformat(config.GEN_START_DATE)
    end=start+timedelta(weeks=config.GEN_WEEKS)
    df=df[(df["sale_date"]>=start)&(df["sale_date"]<end)]
    grouped=df.groupby(["StockCode","sale_date"],as_index=False)["Quantity"].sum()
    top=grouped.groupby("StockCode")["Quantity"].sum().sort_values(ascending=False).head(config.GEN_SKU_LIMIT)
    grouped=grouped[grouped["StockCode"].isin(top.index)]
    price=df.groupby("StockCode")["UnitPrice"].median()
    return grouped,top,price

def scenario_map(sku_codes):
    """按固定比例把 SKU 分配到场景，保证可复现（数量写在 frozen_params 里）。"""
    rng=random.Random(config.GEN_SEED)
    codes=list(sku_codes)
    rng.shuffle(codes)
    plan=[("PROMOTION_SURGE",20),("SUPPLIER_DELAY",20),("NEW_PRODUCT_NO_HISTORY",15),
          ("STOCKOUT_CASCADE",15),("DEMAND_SHIFT",20),("DATA_ANOMALY",10)]
    out={}
    idx=0
    for name,count in plan:
        for code in codes[idx:idx+count]:
            out[str(code)]=name
        idx+=count
    for code in codes[idx:]:
        out[str(code)]="NORMAL"
    return out

def seed_reference(grouped,top,price):
    """建供应商与 SKU（参数为设定值，写进 frozen_params 供审计）。"""
    rng=random.Random(config.GEN_SEED+1)
    suppliers=[]
    for i in range(1,6):
        suppliers.append({
            "supplier_code":"SUP%02d"%i,
            "name":"模拟供应商%02d"%i,
            #设定值：交期 7-21 天，账期 30/45/60，额度 50000-200000
            "lead_time_days":rng.choice([7,10,14,18,21]),
            "lead_time_sigma_days":round(rng.uniform(0.5,3.0),2),
            "payment_terms_days":rng.choice([30,45,60]),
            "credit_limit":float(rng.choice([50000,100000,150000,200000])),
        })
    with db.tx() as cur:
        cur.execute("SET FOREIGN_KEY_CHECKS=0")
        for table in ("replenishment_suggestion","approval_record","inbound_order","purchase_order",
                      "inventory_snapshot","sales_daily","exception_case","policy_snapshot","sku","supplier"):
            cur.execute("DELETE FROM "+table)
        cur.execute("TRUNCATE TABLE audit_event")
        cur.execute("SET FOREIGN_KEY_CHECKS=1")
        for s in suppliers:
            cur.execute("INSERT INTO supplier (supplier_code,name,lead_time_days,lead_time_sigma_days,"
                        "payment_terms_days,credit_limit) VALUES (%s,%s,%s,%s,%s,%s)",
                        (s["supplier_code"],s["name"],s["lead_time_days"],s["lead_time_sigma_days"],
                         s["payment_terms_days"],s["credit_limit"]))
        cur.execute("SELECT id,supplier_code,lead_time_days FROM supplier ORDER BY id")
        supplier_rows=cur.fetchall()
        sku_rows=[]
        for code in top.index:
            sup=rng.choice(supplier_rows)
            median_price=float(price.get(code,10.0)) if price.get(code) else 10.0
            unit_cost=round(max(1.0,median_price*rng.uniform(0.45,0.7)),2)
            sku_rows.append({
                "sku_code":str(code)[:32],
                "name":"商品"+str(code)[:24],
                "category":"GENERAL",
                "supplier_id":sup["id"],
                "unit_cost":unit_cost,
                "price":round(unit_cost*rng.uniform(1.6,2.4),2),
                "lead_time_days":sup["lead_time_days"],
                "moq":rng.choice([1,5,10]),
                "pack_size":rng.choice([1,1,5,10]),
                "service_level":rng.choice([0.90,0.95,0.975]),
            })
        for row in sku_rows:
            cur.execute("INSERT INTO sku (sku_code,name,category,supplier_id,unit_cost,price,"
                        "lead_time_days,moq,pack_size,service_level) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        tuple(row[k] for k in ("sku_code","name","category","supplier_id","unit_cost",
                                               "price","lead_time_days","moq","pack_size","service_level")))
    return suppliers

def transform_qty(scenario,day_index,qty):
    """场景扰动必须落在数据层：Agent 读的是 sales_daily，内存里改是看不见的。

    NEW_PRODUCT 与 DATA_ANOMALY 会写出显式的 0 行，这样"历史很短"与"数据断点"两个信号才可见。
    """
    if scenario=="PROMOTION_SURGE" and 70<=day_index<84:
        return int(qty*3)
    if scenario=="DEMAND_SHIFT" and day_index>=105:
        return int(qty*1.6)
    if scenario=="NEW_PRODUCT_NO_HISTORY" and day_index<140:
        return 0
    #数据异常设计成"每 7 天固定缺 2 天"，这样在任意 7 天窗口里都稳定可见
    if scenario=="DATA_ANOMALY" and day_index%7 in (0,3):
        return 0
    return int(qty)

def load_sales(grouped,scenarios):
    """把选中的销量按场景扰动后写进 sales_daily（PUBLIC），返回 {sku_code: {date: qty}}。"""
    start=date.fromisoformat(config.GEN_START_DATE)
    total_days=(start+timedelta(weeks=config.GEN_WEEKS)-start).days
    mapping={}
    with db.tx() as cur:
        cur.execute("SELECT id,sku_code FROM sku")
        by_code={r["sku_code"]:r["id"] for r in cur.fetchall()}
        for _,row in grouped.iterrows():
            code=str(row["StockCode"])[:32]
            sku_id=by_code.get(code)
            if not sku_id:
                continue
            day_index=(row["sale_date"]-start).days
            qty=transform_qty(scenarios.get(code,"NORMAL"),day_index,int(row["Quantity"]))
            cur.execute("INSERT INTO sales_daily (sku_id,sale_date,qty,source_flag) VALUES (%s,%s,%s,'PUBLIC') "
                        "ON DUPLICATE KEY UPDATE qty=VALUES(qty)",
                        (sku_id,row["sale_date"],qty))
            mapping.setdefault(code,{})[row["sale_date"]]=qty
        #给需要"显式 0 行"的场景补齐缺口：否则稀疏销量会把缺口信号冲掉
        for code,scenario in scenarios.items():
            if scenario not in ("DATA_ANOMALY","NEW_PRODUCT_NO_HISTORY"):
                continue
            sku_id=by_code.get(code)
            if not sku_id:
                continue
            for i in range(total_days):
                if scenario=="DATA_ANOMALY" and i%7 not in (0,3):
                    continue
                if scenario=="NEW_PRODUCT_NO_HISTORY" and i>=140:
                    continue
                day=start+timedelta(days=i)
                cur.execute("INSERT INTO sales_daily (sku_id,sale_date,qty,source_flag) "
                            "VALUES (%s,%s,0,'PUBLIC') ON DUPLICATE KEY UPDATE qty=0",(sku_id,day))
                mapping.setdefault(code,{})[day]=0
    return mapping

def simulate(scenarios,mapping):
    """逐 SKU 逐日模拟：扣减销量、到货入库、触发补货、生成在途与应付。

    关键点：库存与在途由销量推导，保证自洽；场景扰动只作用在需求、交期与历史长度上。
    """
    start=date.fromisoformat(config.GEN_START_DATE)
    end=start+timedelta(weeks=config.GEN_WEEKS)
    rng=random.Random(config.GEN_SEED+2)
    with db.tx() as cur:
        cur.execute("SELECT id,sku_code,supplier_id,unit_cost,lead_time_days,moq,pack_size,service_level "
                    "FROM sku")
        skus=cur.fetchall()
        cur.execute("SELECT id,lead_time_sigma_days FROM supplier")
        sigma_by_sup={r["id"]:float(r["lead_time_sigma_days"]) for r in cur.fetchall()}
    stats={"snapshots":0,"sim_po":0,"inbound":0,"stockout_days":0}
    for sku in skus:
        code=sku["sku_code"]
        scenario=scenarios.get(code,"NORMAL")
        #场景扰动已经在 load_sales 阶段落到 sales_daily，这里只按数据推导库存与在途，避免双重放大
        daily_history=mapping.get(code,{})
        series=[float(daily_history.get(start+timedelta(days=i),0)) for i in range((end-start).days)]
        #模拟器与计算层用同一套策略（含安全库存），否则基线不公平
        hist_window=[q for q in series[:28] if q>0]
        seed_baseline=(sum(hist_window)/len(hist_window)) if hist_window else 1.0
        seed_sigma=compute.stdev(hist_window)
        lead_time=int(sku["lead_time_days"])
        z_value=compute.z_for_service_level(float(sku["service_level"]))
        safety=compute.safety_stock(z_value,seed_sigma,lead_time)
        rop=compute.reorder_point(seed_baseline,lead_time,safety)
        target=compute.target_qty(seed_baseline,lead_time,config.REVIEW_PERIOD_DAYS,z_value,seed_sigma)
        on_hand=int(target+rop)
        if scenario=="STOCKOUT_CASCADE":
            on_hand=max(1,int(on_hand*0.05))
        in_transit=[]
        rows=[]
        po_rows=[]
        for i,day in enumerate([start+timedelta(days=k) for k in range((end-start).days)]):
            demand=series[i]
            on_hand=max(0,on_hand-demand)
            if on_hand==0 and demand>0:
                stats["stockout_days"]+=1
            #先结算到期到货，再把已到货的从在途里移除（顺序写反会导致货永远不到）
            arrived=[x for x in in_transit if x["expected"]<=day]
            on_hand+=sum(x["qty"] for x in arrived)
            in_transit=[x for x in in_transit if x["expected"]>day]
            rows.append((sku["id"],config.WAREHOUSE_ID,day,on_hand,0,
                         sum(x["qty"] for x in in_transit),1))
            position=on_hand+sum(x["qty"] for x in in_transit)
            if position<=rop:
                qty=compute.suggest_qty(position,rop,target,int(sku["moq"]),int(sku["pack_size"]))
                if qty>0:
                    delay=0
                    if scenario=="SUPPLIER_DELAY":
                        delay=rng.randint(7,14)
                    elif scenario=="STOCKOUT_CASCADE":
                        delay=rng.randint(10,20)
                    base_lt=lead_time
                    noise=int(round(rng.gauss(0,sigma_by_sup.get(sku["supplier_id"],1.0))))
                    #承诺交期与实际到货必须分开记：否则"交期偏差"这个信号在数据里看不见
                    promised=day+timedelta(days=max(1,base_lt+noise))
                    arrived=promised+timedelta(days=delay)
                    po_rows.append((sku["id"],sku["supplier_id"],qty,float(sku["unit_cost"]),
                                    round(qty*float(sku["unit_cost"]),2),day,promised,arrived))
                    in_transit.append({"qty":qty,"expected":arrived})
                    stats["sim_po"]+=1
        if rows:
            with db.tx() as cur:
                cur.executemany("INSERT INTO inventory_snapshot (sku_id,warehouse_id,snapshot_date,"
                                "qty_on_hand,qty_reserved,qty_inbound,version) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                                "ON DUPLICATE KEY UPDATE qty_on_hand=VALUES(qty_on_hand),"
                                "qty_inbound=VALUES(qty_inbound)",rows)
                stats["snapshots"]+=len(rows)
                for (sku_id,supplier_id,qty,unit_cost,total,created,promised,arrived) in po_rows:
                    key="SIM:"+hashlib.sha256((str(sku_id)+str(created)).encode()).hexdigest()[:40]
                    cur.execute("INSERT INTO purchase_order (po_no,supplier_id,sku_id,qty,unit_price,"
                                "total_amount,status,idempotency_key,created_at,submitted_at,received_at) "
                                "VALUES (%s,%s,%s,%s,%s,%s,'RECEIVED',%s,%s,%s,%s)",
                                ("SIM-"+sku["sku_code"][:10]+"-"+str(created),supplier_id,sku_id,qty,
                                 unit_cost,total,key,created,created,arrived))
                    po_id=cur.lastrowid
                    cur.execute("INSERT INTO inbound_order (sku_id,po_id,qty,expected_date,actual_date,status) "
                                "VALUES (%s,%s,%s,%s,%s,'RECEIVED')",(sku_id,po_id,qty,promised,arrived))
                    stats["inbound"]+=1
    return stats

def freeze_params(scenarios,extra=None):
    """把生成与计算参数冻结成 config_version + 本地文件，作为预注册凭证。"""
    params={
        "seed":config.GEN_SEED,"sku_limit":config.GEN_SKU_LIMIT,
        "start_date":config.GEN_START_DATE,"weeks":config.GEN_WEEKS,
        "baseline_window_days":config.BASELINE_WINDOW_DAYS,
        "review_period_days":config.REVIEW_PERIOD_DAYS,
        "order_cost":config.ORDER_COST,"holding_rate":config.HOLDING_RATE,
        "inventory_cap_multiplier":config.INVENTORY_CAP_MULTIPLIER,
        "anomaly_enter_score":config.ANOMALY_ENTER_SCORE,
        "anomaly_exit_score":config.ANOMALY_EXIT_SCORE,
        "anomaly_confirm_runs":config.ANOMALY_CONFIRM_DAYS,
        "anomaly_daily_quota":config.ANOMALY_DAILY_QUOTA,
        "approver_amount_limit":config.APPROVER_AMOUNT_LIMIT,
        "warehouse_id":config.WAREHOUSE_ID,
        "scenario_counts":{name:sum(1 for v in scenarios.values() if v==name) for name in SCENARIOS},
        "source":"UCI Online Retail (CC BY 4.0) 作为需求信号；库存/在途/交期/应付为模拟推导",
    }
    if extra:
        params.update(extra)
    repository.upsert_config_version("generator",config.RULE_VERSION,params,note="数据集构建时冻结")
    config.DATA_DIR.mkdir(parents=True,exist_ok=True)
    (config.DATA_DIR/"frozen_params.json").write_text(json.dumps(params,ensure_ascii=False,indent=2),
                                                     encoding="utf-8")
    (config.DATA_DIR/"scenarios.json").write_text(json.dumps(scenarios,ensure_ascii=False,indent=2),
                                                  encoding="utf-8")
    return params

def build(force_download=False):
    xlsx=download(force=force_download)
    grouped,top,price=select_sales(xlsx)
    scenarios=scenario_map(top.index)
    seed_reference(grouped,top,price)
    mapping=load_sales(grouped,scenarios)
    stats=simulate(scenarios,mapping)
    params=freeze_params(scenarios)
    summary={"skus":len(top),"sales_rows":int(len(grouped)),"sim":stats,
             "scenario_counts":params["scenario_counts"]}
    return summary

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--download",action="store_true")
    parser.add_argument("--build",action="store_true")
    parser.add_argument("--force-download",action="store_true")
    args=parser.parse_args()
    if args.download:
        print("下载完成:",download(force=args.force_download))
    if args.build:
        print("构建完成:",json.dumps(build(force_download=args.force_download),ensure_ascii=False))
    if not args.download and not args.build:
        parser.print_help()

if __name__=="__main__":
    main()
