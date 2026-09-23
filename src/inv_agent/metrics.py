"""复算：缺货率、周转天数、超储金额。

契约 10.6 第二档：这些是仿真 KPI，必须标注仿真口径，不做因果主张。
"""
METRIC_DEFINITION_VERSION="v1"

def period_stockout_rate(period_start,period_end,warehouse_id):
    """缺货率＝有需求且库存为 0 的 SKU-天 / 有需求的 SKU-天。"""
    row=None
    from inv_agent import db
    row=db.query_one(
        "SELECT COUNT(*) AS demand_days,"
        " SUM(CASE WHEN qty>0 AND COALESCE(inv.qty_on_hand,0)-COALESCE(inv.qty_reserved,0)<=0 THEN 1 ELSE 0 END) AS stockout_days "
        "FROM sales_daily s LEFT JOIN inventory_snapshot inv ON inv.sku_id=s.sku_id AND inv.snapshot_date=s.sale_date "
        "WHERE s.qty>0 AND s.sale_date BETWEEN %s AND %s AND COALESCE(inv.warehouse_id,%s)=%s",
        (period_start,period_end,warehouse_id,warehouse_id))
    demand_days=int(row["demand_days"] or 0)
    stockout_days=int(row["stockout_days"] or 0)
    return {"stockout_rate":round(stockout_days/demand_days,4) if demand_days else 0.0,
            "demand_sku_days":demand_days,"stockout_sku_days":stockout_days}

def period_turnover_days(period_start,period_end,warehouse_id):
    """周转天数：按 SKU 分别算平均库存 / 日均销量再取 SKU 均值；另给全仓合计口径。

    口径写死：库存取在手（不含在途）；只统计期内有销量的 SKU。
    """
    from inv_agent import db
    rows=db.query_all(
        "SELECT i.sku_id, AVG(i.qty_on_hand) AS avg_inv, "
        " (SELECT SUM(s.qty) FROM sales_daily s WHERE s.sku_id=i.sku_id AND s.qty>0 "
        "  AND s.sale_date BETWEEN %s AND %s) AS total_qty, "
        " (SELECT COUNT(DISTINCT s.sale_date) FROM sales_daily s WHERE s.sku_id=i.sku_id AND s.qty>0 "
        "  AND s.sale_date BETWEEN %s AND %s) AS sale_days "
        "FROM inventory_snapshot i WHERE i.warehouse_id=%s AND i.snapshot_date BETWEEN %s AND %s "
        "GROUP BY i.sku_id",
        (period_start,period_end,period_start,period_end,warehouse_id,period_start,period_end))
    per_sku=[]
    total_inv=0.0
    total_qty=0
    total_days=0
    for r in rows:
        avg_inv=float(r["avg_inv"] or 0)
        qty=int(r["total_qty"] or 0)
        days=int(r["sale_days"] or 0)
        total_inv+=avg_inv
        total_qty+=qty
        total_days+=days
        if qty>0 and days>0:
            per_sku.append(avg_inv/(qty/days))
    mean_turnover=sum(per_sku)/len(per_sku) if per_sku else 0.0
    #少数批发型 SKU 的库存深度被极端订单抬高，均值会被离群值带偏，所以同时给中位数
    per_sku_sorted=sorted(per_sku)
    if per_sku_sorted:
        mid=len(per_sku_sorted)//2
        median_turnover=(per_sku_sorted[mid] if len(per_sku_sorted)%2
                         else (per_sku_sorted[mid-1]+per_sku_sorted[mid])/2)
    else:
        median_turnover=0.0
    aggregate=total_inv/(total_qty/total_days) if total_qty and total_days else 0.0
    return {"turnover_days":round(mean_turnover,2),
            "turnover_days_median":round(median_turnover,2),
            "turnover_days_aggregate":round(aggregate,2),
            "turnover_sku_count":len(per_sku),
            "avg_inventory":round(total_inv/len(rows),2) if rows else 0.0}

def period_excess_amount(warehouse_id,period_end):
    """超储金额＝Σ max(最新在手库存 − 目标库存, 0) × 单位成本。"""
    from inv_agent import db
    rows=db.query_all(
        "SELECT k.unit_cost, inv.qty_on_hand, COALESCE(p.target_qty,0) AS target_qty "
        "FROM sku k JOIN inventory_snapshot inv ON inv.sku_id=k.id AND inv.warehouse_id=%s "
        "AND inv.snapshot_date=(SELECT MAX(snapshot_date) FROM inventory_snapshot WHERE sku_id=k.id AND warehouse_id=%s) "
        "LEFT JOIN policy_snapshot p ON p.sku_id=k.id AND p.id=(SELECT MAX(id) FROM policy_snapshot WHERE sku_id=k.id)",
        (warehouse_id,warehouse_id))
    total=0.0
    for r in rows:
        exceed=int(r["qty_on_hand"])-int(r["target_qty"] or 0)
        if exceed>0:
            total+=exceed*float(r["unit_cost"])
    return {"excess_amount":round(total,2),"sku_count":len(rows)}

def recompute(period_start,period_end,warehouse_id):
    result=period_stockout_rate(period_start,period_end,warehouse_id)
    result.update(period_turnover_days(period_start,period_end,warehouse_id))
    result.update(period_excess_amount(warehouse_id,period_end))
    result["definition_version"]=METRIC_DEFINITION_VERSION
    result["period_start"]=str(period_start)
    result["period_end"]=str(period_end)
    result["warehouse_id"]=warehouse_id
    return result
