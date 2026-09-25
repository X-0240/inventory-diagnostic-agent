"""命令行入口：种子数据 → 批量计划 → 审批 → 执行 → 复算 → 恢复。

用法示例（项目根目录执行，先设 PYTHONPATH=src）：
  python -m inv_agent.cli seed
  python -m inv_agent.cli plan-period --period 2011-W20
  python -m inv_agent.cli list --status PENDING_APPROVAL
  python -m inv_agent.cli approve --suggestion 1 --decision APPROVE --actor alice --role APPROVER
  python -m inv_agent.cli execute --suggestion 1 --actor alice --role APPROVER
  python -m inv_agent.cli report --period 2011-W20
  python -m inv_agent.cli recover
"""
import argparse
import hashlib
import json
import sys
from datetime import timedelta

from inv_agent import (anomaly, config, data_pipeline, executor, graph, intake, metrics, pipeline,
                       repository)

def cmd_seed(args):
    print(json.dumps(data_pipeline.build(force_download=args.force),ensure_ascii=False,indent=2))

def _prev_period(period):
    start,_=pipeline.period_bounds(period)
    return pipeline.period_of(start-timedelta(days=7))

def cmd_plan_period(args):
    print(json.dumps(graph.run_period(args.period,limit=args.limit,quota=args.quota,
                                      force=bool(args.force),workers=args.workers or 1),
                     ensure_ascii=False,indent=2))

def cmd_list(args):
    rows=repository.list_suggestions(status=args.status,period=args.period,limit=args.limit)
    out=[{"id":r["id"],"sku":r["sku_code"],"period":r["period"],"qty":r["qty"],
          "amount":float(r["amount"]),"status":r["status"],"hypothesis":r["hypothesis_type"],
          "confidence":float(r["confidence"]) if r["confidence"] is not None else None,
          "hash":r["content_hash"][:12]} for r in rows]
    print(json.dumps(out,ensure_ascii=False,indent=2))

def cmd_approve(args):
    suggestion=repository.get_suggestion(args.suggestion)
    if not suggestion:
        print(json.dumps({"error":"NOT_FOUND"},ensure_ascii=False)); return
    if args.decision=="MODIFY":
        if not args.qty:
            print(json.dumps({"error":"RULE_VIOLATION","message":"MODIFY 必须给 --qty"},
                             ensure_ascii=False)); return
        sku=repository.get_sku(suggestion["sku_id"])
        amount=round(args.qty*float(sku["unit_cost"]),2)
        repository.set_status(suggestion["id"],"PENDING_APPROVAL","SUPERSEDED")
        raw="|".join([str(suggestion["sku_id"]),suggestion["period"],str(args.qty),str(amount),
                      config.RULE_VERSION,"modified"])
        new_id=repository.insert_suggestion(
            sku_id=suggestion["sku_id"],period=suggestion["period"],qty=args.qty,amount=amount,
            basis=_as_json(suggestion["basis_json"]),rule_trace=_as_json(suggestion["rule_trace_json"]),
            content_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            case_id=suggestion["case_id"],hypothesis=suggestion["hypothesis_type"],
            status="PENDING_APPROVAL")
        repository.audit("suggestion",suggestion["id"],"MODIFIED",args.actor,args.role,
                         {"new_suggestion_id":new_id,"qty":args.qty,"amount":amount})
        print(json.dumps({"modified":True,"new_suggestion_id":new_id,"qty":args.qty,
                          "amount":amount},ensure_ascii=False)); return
    print(json.dumps(graph.run_by_id(args.suggestion,args.decision,args.actor,args.role,
                                     execute=not args.no_execute),
                     ensure_ascii=False,indent=2))

def _as_json(value):
    return value if isinstance(value,(dict,list)) else json.loads(value)

def cmd_execute(args):
    try:
        result=executor.submit(args.suggestion,args.actor,args.role)
    except Exception as e:
        print(json.dumps({"error":getattr(e,"code","INTERNAL_ERROR"),"message":str(e)},
                         ensure_ascii=False)); return
    print(json.dumps(result,ensure_ascii=False,indent=2))

def cmd_recover(args):
    print(json.dumps(executor.recover(),ensure_ascii=False,indent=2))

def cmd_ask(args):
    """自然语言入口：解析成结构化参数；--run 时才真的跑一次计划。"""
    result=intake.answer(args.text) if args.run else intake.parse(args.text)
    print(json.dumps(result,ensure_ascii=False,indent=2))

def cmd_report(args):
    start,end=pipeline.period_bounds(args.period)
    result=metrics.recompute(start,end,config.WAREHOUSE_ID)
    rows=repository.list_suggestions(period=args.period,limit=1000)
    by_status={}
    for r in rows:
        by_status[r["status"]]=by_status.get(r["status"],0)+1
    result["suggestions_by_status"]=by_status
    result["suggestion_total"]=len(rows)
    print(json.dumps(result,ensure_ascii=False,indent=2))

def cmd_graph_run(args):
    result=graph.run(args.sku_code,args.period,mode=args.mode,diagnose=args.diagnose,
                     actor=args.actor,role=args.role)
    print(json.dumps(_short(result),ensure_ascii=False,indent=2))

def cmd_graph_resume(args):
    result=graph.resume(args.sku_code,args.period,args.decision,args.actor,args.role)
    print(json.dumps(_short(result),ensure_ascii=False,indent=2))

def _short(result):
    out={k:v for k,v in result.items()
         if k in ("errors","approval","execution","recompute","suggestion_id","guardrails")}
    if "__interrupt__" in result:
        out["interrupt"]=True
    return out

def build_parser():
    p=argparse.ArgumentParser(prog="inv_agent")
    sub=p.add_subparsers(dest="cmd")
    s=sub.add_parser("seed"); s.add_argument("--force",action="store_true"); s.set_defaults(func=cmd_seed)
    s=sub.add_parser("plan-period"); s.add_argument("--period",required=True)
    s.add_argument("--quota",type=int); s.add_argument("--limit",type=int)
    s.add_argument("--force",action="store_true",help="忽略已成功或运行中的任务，强制重跑")
    s.add_argument("--workers",type=int,default=1,help="并发跑多少个 SKU（诊断要调模型，并发能显著缩短整体耗时）")
    s.set_defaults(func=cmd_plan_period)
    s=sub.add_parser("list"); s.add_argument("--status"); s.add_argument("--period")
    s.add_argument("--limit",type=int,default=50); s.set_defaults(func=cmd_list)
    s=sub.add_parser("approve"); s.add_argument("--suggestion",type=int,required=True)
    s.add_argument("--decision",choices=["APPROVE","REJECT","MODIFY"],required=True)
    s.add_argument("--qty",type=int); s.add_argument("--actor",default="alice")
    s.add_argument("--role",default="APPROVER"); s.add_argument("--no-execute",action="store_true")
    s.set_defaults(func=cmd_approve)
    s=sub.add_parser("execute"); s.add_argument("--suggestion",type=int,required=True)
    s.add_argument("--actor",default="alice"); s.add_argument("--role",default="APPROVER")
    s.set_defaults(func=cmd_execute)
    s=sub.add_parser("recover"); s.set_defaults(func=cmd_recover)
    s=sub.add_parser("ask"); s.add_argument("--text",required=True); s.add_argument("--run",action="store_true")
    s.set_defaults(func=cmd_ask)
    s=sub.add_parser("report"); s.add_argument("--period",required=True); s.set_defaults(func=cmd_report)
    s=sub.add_parser("graph-run"); s.add_argument("--sku-code",required=True)
    s.add_argument("--period",required=True); s.add_argument("--mode",default="approve")
    s.add_argument("--diagnose",action="store_true"); s.add_argument("--actor",default="planner")
    s.add_argument("--role",default="BUYER"); s.set_defaults(func=cmd_graph_run)
    s=sub.add_parser("graph-resume"); s.add_argument("--sku-code",required=True)
    s.add_argument("--period",required=True)
    s.add_argument("--decision",choices=["APPROVE","REJECT"],required=True)
    s.add_argument("--actor",default="alice"); s.add_argument("--role",default="APPROVER")
    s.set_defaults(func=cmd_graph_resume)
    return p

def main(argv=None):
    parser=build_parser()
    args=parser.parse_args(argv)
    if not getattr(args,"func",None):
        parser.print_help()
        return 1
    args.func(args)
    return 0

if __name__=="__main__":
    sys.exit(main())
