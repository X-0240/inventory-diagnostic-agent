"""数据访问层：把 SQL 集中在这里，业务逻辑不直接拼 SQL。"""
import json
from datetime import date, datetime
from decimal import Decimal

import pymysql

from inv_agent import db

def json_default(obj):
    """Decimal / date 的 JSON 兜底：数据库数值列是 Decimal，直接序列化会报错。"""
    if isinstance(obj,Decimal):
        return float(obj)
    if isinstance(obj,(date,datetime)):
        return obj.isoformat()
    raise TypeError("不支持的 JSON 类型: "+type(obj).__name__)

def dumps(obj):
    return json.dumps(obj,ensure_ascii=False,default=json_default)

def active_skus(limit=None):
    #只取有库存快照的 SKU：测试残留或半灌数据不会进批量扫描，避免刷一堆 NOT_FOUND
    sql=("SELECT k.id,k.sku_code,k.name,k.supplier_id,k.unit_cost,k.price,k.lead_time_days,"
         "k.moq,k.pack_size,k.service_level FROM sku k "
         "WHERE k.status='ACTIVE' AND EXISTS (SELECT 1 FROM inventory_snapshot i WHERE i.sku_id=k.id) "
         "ORDER BY k.id")
    if limit:
        sql+=" LIMIT "+str(int(limit))
    return db.query_all(sql)

def get_sku(sku_id):
    return db.query_one("SELECT * FROM sku WHERE id=%s",(sku_id,))

def get_supplier(supplier_id):
    return db.query_one("SELECT * FROM supplier WHERE id=%s",(supplier_id,))

def latest_inventory(sku_id,warehouse_id):
    return db.query_one("SELECT * FROM inventory_snapshot WHERE sku_id=%s AND warehouse_id=%s "
                        "ORDER BY snapshot_date DESC LIMIT 1",(sku_id,warehouse_id))

def inbound_due(sku_id,until_date):
    rows=db.query_all("SELECT COALESCE(SUM(qty),0) AS qty FROM inbound_order "
                      "WHERE sku_id=%s AND status='IN_TRANSIT' AND expected_date<=%s",
                      (sku_id,until_date))
    return int(rows[0]["qty"]) if rows else 0

def inbound_all(sku_id):
    return db.query_all("SELECT id,po_id,qty,expected_date,status FROM inbound_order "
                        "WHERE sku_id=%s AND status='IN_TRANSIT' ORDER BY expected_date",(sku_id,))

def sales_series(sku_id,end_date,days):
    rows=db.query_all("SELECT sale_date,qty FROM sales_daily WHERE sku_id=%s AND sale_date<=%s "
                      "ORDER BY sale_date DESC LIMIT "+str(int(days)),(sku_id,end_date))
    return list(reversed(rows))

def open_po_amount(supplier_id):
    rows=db.query_all("SELECT COALESCE(SUM(total_amount),0) AS amount FROM purchase_order "
                      "WHERE supplier_id=%s AND status IN ('DRAFT','SUBMITTED','CONFIRMED')",(supplier_id,))
    return float(rows[0]["amount"]) if rows else 0.0

def upsert_policy_snapshot(sku_id,period,payload):
    with db.tx() as cur:
        cur.execute("INSERT INTO policy_snapshot (sku_id,period,method,baseline_daily,sigma,sample_days,"
                    "service_level,z,safety_qty,reorder_point,target_qty,rule_version) "
                    "VALUES (%(sku_id)s,%(period)s,%(method)s,%(baseline_daily)s,%(sigma)s,%(sample_days)s,"
                    "%(service_level)s,%(z)s,%(safety_qty)s,%(reorder_point)s,%(target_qty)s,%(rule_version)s) "
                    "ON DUPLICATE KEY UPDATE baseline_daily=VALUES(baseline_daily),sigma=VALUES(sigma),"
                    "sample_days=VALUES(sample_days),safety_qty=VALUES(safety_qty),"
                    "reorder_point=VALUES(reorder_point),target_qty=VALUES(target_qty)",
                    dict(payload,sku_id=sku_id,period=period))

def get_case(sku_id,period):
    return db.query_one("SELECT * FROM exception_case WHERE sku_id=%s AND period=%s",(sku_id,period))

def open_case(sku_id,period,hypothesis,confidence,status="INVESTIGATING"):
    with db.tx() as cur:
        cur.execute("INSERT INTO exception_case (sku_id,period,status,hypothesis_type,confidence) "
                    "VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE status=VALUES(status),"
                    "hypothesis_type=VALUES(hypothesis_type),confidence=VALUES(confidence),version=version+1",
                    (sku_id,period,status,hypothesis,confidence))
        cur.execute("SELECT id FROM exception_case WHERE sku_id=%s AND period=%s",(sku_id,period))
        return cur.fetchone()["id"]

def takeover_case(case_id,reason):
    with db.tx() as cur:
        cur.execute("UPDATE exception_case SET status='MANUAL_TAKEOVER',owner=COALESCE(owner,%s) "
                    "WHERE id=%s AND status IN ('OPEN','INVESTIGATING')",(reason,case_id))
        return cur.rowcount

def active_suggestion(sku_id,period):
    return db.query_one("SELECT * FROM replenishment_suggestion WHERE sku_id=%s AND period=%s "
                        "AND active_flag=1",(sku_id,period))

def suggestion_by_hash(sku_id,period,content_hash):
    """任何状态的同内容建议：用于幂等判定（唯一键 uk_sug_sku_period_hash 挡的就是它）。"""
    return db.query_one("SELECT id,status FROM replenishment_suggestion "
                        "WHERE sku_id=%s AND period=%s AND content_hash=%s",
                        (sku_id,period,content_hash))

def enrich_diagnosis(suggestion_id,diagnosis,case_id=None):
    """给已存在的建议补写归因结论（幂等重放场景）。

    只补不覆盖：仅当 hypothesis_type 为空时写入，避免把已经定稿的归因改掉。
    """
    with db.tx() as cur:
        cur.execute("UPDATE replenishment_suggestion SET hypothesis_type=%s,evidence_refs=%s,"
                    "conflicting_evidence=%s,proposed_actions=%s,confidence=%s,"
                    "case_id=COALESCE(case_id,%s) WHERE id=%s AND hypothesis_type IS NULL",
                    (diagnosis["hypothesis_type"],dumps(diagnosis["evidence_refs"]),
                     1 if diagnosis["conflicting_evidence"] else 0,
                     dumps(diagnosis["proposed_actions"]),float(diagnosis["confidence"]),
                     case_id,suggestion_id))
        return cur.rowcount

def get_suggestion(suggestion_id):
    return db.query_one("SELECT * FROM replenishment_suggestion WHERE id=%s",(suggestion_id,))

def insert_suggestion(sku_id,period,qty,amount,basis,rule_trace,content_hash,
                      case_id=None,hypothesis=None,evidence=None,conflicting=False,
                      proposed_actions=None,confidence=None,status="DRAFT"):
    try:
        with db.tx() as cur:
            cur.execute("INSERT INTO replenishment_suggestion (sku_id,period,case_id,qty,amount,basis_json,"
                        "rule_trace_json,hypothesis_type,evidence_refs,conflicting_evidence,proposed_actions,"
                        "confidence,content_hash,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (sku_id,period,case_id,qty,amount,dumps(basis),
                         dumps(rule_trace),hypothesis,
                         dumps(evidence) if evidence is not None else None,
                         1 if conflicting else 0,
                         dumps(proposed_actions) if proposed_actions is not None else None,
                         confidence,content_hash,status))
            return cur.lastrowid
    except pymysql.err.IntegrityError as e:
        #幂等兜底：同 SKU 同周期同内容已存在时返回原纪录，不报错也不新建
        if e.args and e.args[0]==1062:
            row=suggestion_by_hash(sku_id,period,content_hash)
            if row:
                return row["id"]
        raise

def set_status(suggestion_id,from_status,to_status,content_hash=None):
    """条件状态更新：rowcount=0 说明状态已被别人改过，调用方按 STATE_CONFLICT 处理。"""
    sql="UPDATE replenishment_suggestion SET status=%s WHERE id=%s AND status=%s"
    params=[to_status,suggestion_id,from_status]
    if content_hash is not None:
        sql+=" AND content_hash=%s"
        params.append(content_hash)
    with db.tx() as cur:
        cur.execute(sql,tuple(params))
        return cur.rowcount

def supersede_others(sku_id,period,keep_id):
    with db.tx() as cur:
        cur.execute("UPDATE replenishment_suggestion SET status='SUPERSEDED' WHERE sku_id=%s AND period=%s "
                    "AND id<>%s AND active_flag=1",(sku_id,period,keep_id))
        return cur.rowcount

def list_suggestions(status=None,period=None,limit=100):
    sql=("SELECT s.*,k.sku_code FROM replenishment_suggestion s JOIN sku k ON k.id=s.sku_id WHERE 1=1")
    params=[]
    if status:
        sql+=" AND s.status=%s"
        params.append(status)
    if period:
        sql+=" AND s.period=%s"
        params.append(period)
    sql+=" ORDER BY s.amount DESC, s.id LIMIT "+str(int(limit))
    return db.query_all(sql,tuple(params))

def record_approval(suggestion_id,decision,actor,role,content_hash,snapshot,comment=None):
    with db.tx() as cur:
        cur.execute("INSERT INTO approval_record (suggestion_id,decision,decided_by,decided_role,"
                    "content_hash,approval_snapshot_json,comment) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (suggestion_id,decision,actor,role,content_hash,
                     dumps(snapshot),comment))
        return cur.lastrowid

def insert_po(supplier_id,sku_id,qty,unit_price,total_amount,suggestion_id,
              idempotency_key,snapshot,po_no,reverse_of_po_id=None):
    with db.tx() as cur:
        cur.execute("INSERT INTO purchase_order (po_no,suggestion_id,supplier_id,sku_id,qty,unit_price,"
                    "total_amount,status,idempotency_key,approval_snapshot_json,reverse_of_po_id,submitted_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,'SUBMITTED',%s,%s,%s,UTC_TIMESTAMP())",
                    (po_no,suggestion_id,supplier_id,sku_id,qty,unit_price,total_amount,
                     idempotency_key,dumps(snapshot),reverse_of_po_id))
        return cur.lastrowid

def find_po_by_idem(idempotency_key):
    return db.query_one("SELECT * FROM purchase_order WHERE idempotency_key=%s",(idempotency_key,))

def next_po_no(period,sku_code):
    row=db.query_one("SELECT COUNT(*) AS n FROM purchase_order WHERE po_no LIKE %s",("PO-"+period+"-%",))
    return "PO-"+period+"-"+str((row["n"] if row else 0)+1).zfill(4)+"-"+sku_code[:12]

def audit(entity,entity_id,event_type,actor,role,payload=None):
    db.execute("INSERT INTO audit_event (entity,entity_id,event_type,actor,actor_role,payload_json) "
               "VALUES (%s,%s,%s,%s,%s,%s)",
               (entity,entity_id,event_type,actor,role,
                dumps(payload) if payload is not None else None))

def job_begin(job_name,period,force=False,stale_minutes=30):
    """UNIQUE(job_name,period) 兼作锁。

    阻塞条件（返回 None）：存在 SUCCEEDED 的行，或存在仍在 stale_minutes 内的 RUNNING 行。
    FAILED、僵尸 RUNNING（超时）与 force=True 都允许重跑，避免一次崩溃把周期永久锁死。
    """
    try:
        with db.tx() as cur:
            cur.execute("INSERT INTO job_run (job_name,period,status) VALUES (%s,%s,'RUNNING')",
                        (job_name,period))
            return cur.lastrowid
    except Exception:
        row=db.query_one("SELECT id,status,started_at FROM job_run WHERE job_name=%s AND period=%s",
                         (job_name,period))
        if not row:
            return None
        if row["status"]=="SUCCEEDED" and not force:
            return None
        if row["status"]=="RUNNING" and not force:
            age=db.query_one("SELECT TIMESTAMPDIFF(MINUTE,started_at,UTC_TIMESTAMP()) AS minutes FROM job_run WHERE id=%s",
                             (row["id"],))
            if age and age["minutes"] is not None and age["minutes"]<stale_minutes:
                return None
        db.execute("UPDATE job_run SET status='RUNNING',retry_count=retry_count+1,"
                   "started_at=UTC_TIMESTAMP(),finished_at=NULL WHERE id=%s",(row["id"],))
        return row["id"]

def job_finish(job_id,status,stats,error_code=None,last_error=None,side_effect="NONE",reconcile=None):
    db.execute("UPDATE job_run SET status=%s,finished_at=UTC_TIMESTAMP(),stats_json=%s,"
               "error_code=%s,last_error=%s,side_effect_status=%s,reconcile_action=%s WHERE id=%s",
               (status,dumps(stats),error_code,last_error,
                side_effect,reconcile,job_id))

def prev_job_stats(job_name,period):
    row=db.query_one("SELECT stats_json FROM job_run WHERE job_name=%s AND period=%s",(job_name,period))
    if not row or not row["stats_json"]:
        return {}
    return row["stats_json"] if isinstance(row["stats_json"],dict) else json.loads(row["stats_json"])

def upsert_config_version(config_key,rule_version,params,note=None):
    db.execute("INSERT INTO config_version (config_key,rule_version,params_json,frozen_by,note) "
               "VALUES (%s,%s,%s,'system',%s) ON DUPLICATE KEY UPDATE params_json=VALUES(params_json)",
               (config_key,rule_version,dumps(params),note))

def get_config_version(config_key,rule_version):
    row=db.query_one("SELECT * FROM config_version WHERE config_key=%s AND rule_version=%s",
                     (config_key,rule_version))
    if not row:
        return None
    return row["params_json"] if isinstance(row["params_json"],dict) else json.loads(row["params_json"])
