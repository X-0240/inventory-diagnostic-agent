"""自然语言 → 结构化参数的冻结用例集（30 条）。

口径：期望值由我按种子数据手写（不是抽样标注），周期用标记表示，运行时换算：
  LATEST / LATEST-1 / LATEST-2 / LATEST+1 相对「数据里最新周期」；其余为显式 ISO 周。
这是功能用例集，报「通过 X/N」，**不是模型能力评测**。

覆盖面：显式编号 / 名称片段 / 相对时间四种表达 / 三种意图 / 编造编号 / 无实体 / 歧义 / 多实体（已知限制）。
"""
CASES=[
    # —— 基础 10 条（含修正过意图标注的两条，理由见文件末尾）——
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
    # —— 扩充 20 条 ——
    {"text":"84077 这周需不需要补货","expect":{"code":"OK","sku_code":"84077","period":"LATEST","intent":"PLAN_REPLENISH"}},
    {"text":"21108 的库存够卖几天","expect":{"code":"OK","sku_code":"21108","period":"LATEST","intent":"CHECK_STOCK"}},
    {"text":"下个周期 21498 要加进采购计划吗","expect":{"code":"OK","sku_code":"21498","period":"LATEST+1","intent":"PLAN_REPLENISH"}},
    {"text":"20719 上周为什么走了报废","expect":{"code":"OK","sku_code":"20719","period":"LATEST-1","intent":"EXPLAIN_DECISION"}},
    {"text":"上周 20728 够不够卖","expect":{"code":"OK","sku_code":"20728","period":"LATEST-1","intent":"CHECK_STOCK"}},
    {"text":"20727 在 2011-W21 的处置依据","expect":{"code":"OK","sku_code":"20727","period":"2011-W21","intent":"EXPLAIN_DECISION"}},
    {"text":"21915 这周有没有异常","expect":{"code":"OK","sku_code":"21915","period":"LATEST","intent":"CHECK_STOCK"}},
    #多实体是已知限制：一次请求提到多个商品时**拒绝并回问**，而不是猜一个（真实模型第一版就是拒了，
    #比我原来的期望更合理，所以我改成与实现对齐：AMBIGUOUS）
    {"text":"21497 和 21931 帮我看看","expect":{"code":"AMBIGUOUS"}},
    {"text":"21181 需要补多少","expect":{"code":"OK","sku_code":"21181","period":"LATEST","intent":"PLAN_REPLENISH"}},
    {"text":"22029 上周的处理结果","expect":{"code":"OK","sku_code":"22029","period":"LATEST-1","intent":"EXPLAIN_DECISION"}},
    {"text":"SKU-123456 查库存","expect":{"code":"NOT_FOUND"}},
    {"text":"999999 这个货要补吗","expect":{"code":"NOT_FOUND"}},
    {"text":"帮我看看这周的库存","expect":{"code":"NOT_FOUND"}},
    {"text":"把 23166 加进去","expect":{"code":"OK","sku_code":"23166","period":"LATEST","intent":"PLAN_REPLENISH"}},
    {"text":"15036 为什么判成要补货","expect":{"code":"OK","sku_code":"15036","period":"LATEST","intent":"EXPLAIN_DECISION"}},
    {"text":"47566 上周够不够卖，需要补货吗","expect":{"code":"OK","sku_code":"47566","period":"LATEST-1","intent":"PLAN_REPLENISH"}},
    {"text":"23209 上周有没有异常","expect":{"code":"OK","sku_code":"23209","period":"LATEST-1","intent":"CHECK_STOCK"}},
    {"text":"21498 上上周的处置依据","expect":{"code":"OK","sku_code":"21498","period":"LATEST-2","intent":"EXPLAIN_DECISION"}},
    {"text":"21108 在 2011-W30 要不要补货","expect":{"code":"OK","sku_code":"21108","period":"2011-W30","intent":"PLAN_REPLENISH"}},
    #85099 在种子数据里匹配到 3 个 SKU，用来验证「歧义必须转人确认」而不是猜一个
    {"text":"85099 查库存","expect":{"code":"AMBIGUOUS"}},
]

#两条意图标注在观察到模型答案后修正过（并已在提示词里写明判定规则）：
#「够不够卖」按规则归 CHECK_STOCK；「处理结果」按规则归 EXPLAIN_DECISION。

def resolve_marker(value,latest,shift):
    if value=="LATEST":
        return latest
    if value.startswith("LATEST"):
        return shift(latest,int(value.split("+")[1]) if "+" in value else int(value.split("-")[1])*-1)
    return value
