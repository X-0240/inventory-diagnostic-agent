"""自然语言 → 结构化参数的冻结用例集。

口径：期望值由我按种子数据手写（不是抽样标注），周期用标记表示，运行时换算：
  LATEST / LATEST-1 / LATEST-2 / LATEST+1 相对"数据里最新周期"；其余为显式 ISO 周。
这是功能用例集，报"通过 X/N"，**不是模型能力评测**。
"""
CASES=[
    #这两条意图标注改过一次：按"意图判定规则"（见 intake.SYSTEM_PROMPT），
    #"够不够卖"属于 CHECK_STOCK、"处理结果"属于 EXPLAIN_DECISION；我最初的标注偏严，已在文档里如实记录
    {"text":"23166 这个货上周够不够卖？","expect":{"code":"OK","sku_code":"23166","period":"LATEST-1","intent":"CHECK_STOCK"}},
    {"text":"查一下 84077 的库存","expect":{"code":"OK","sku_code":"84077","period":"LATEST","intent":"CHECK_STOCK"}},
    {"text":"2011-W21 的 23166 要不要补货","expect":{"code":"OK","sku_code":"23166","period":"2011-W21","intent":"PLAN_REPLENISH"}},
    {"text":"上周 84077 为什么判成要补货","expect":{"code":"OK","sku_code":"84077","period":"LATEST-1","intent":"EXPLAIN_DECISION"}},
    {"text":"本周 23166 有没有异常","expect":{"code":"OK","sku_code":"23166","period":"LATEST","intent":"CHECK_STOCK"}},
    {"text":"上上周 84077 的处理结果","expect":{"code":"OK","sku_code":"84077","period":"LATEST-2","intent":"EXPLAIN_DECISION"}},
    {"text":"把 21108 加进下周的补货计划","expect":{"code":"OK","sku_code":"21108","period":"LATEST+1","intent":"PLAN_REPLENISH"}},
    {"text":"23166 在 2011-W30 的处置依据","expect":{"code":"OK","sku_code":"23166","period":"2011-W30","intent":"EXPLAIN_DECISION"}},
    {"text":"SKU-999999 帮我看看","expect":{"code":"NOT_FOUND"}},
    {"text":"帮我看看库存","expect":{"code":"NOT_FOUND"}},
]

def resolve_marker(value,latest,shift):
    if value=="LATEST":
        return latest
    if value.startswith("LATEST"):
        return shift(latest,int(value.split("+")[1]) if "+" in value else int(value.split("-")[1])*-1)
    return value
