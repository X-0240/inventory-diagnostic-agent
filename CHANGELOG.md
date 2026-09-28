# 更新日志（CHANGELOG）

本文件记录每一版“改了什么、为什么改”。上手与运行方式见 [README.md](README.md)，
实测数字与口径见 [docs/证据与局限.md](docs/证据与局限.md)。

## v1.1 变更（相对 v1）

| 项 | 变更 | 证据位置 |
|---|---|---|
| 离散度估计 | 改用**稳健 σ**：间歇需求用"发生-规模"模型，其余用 MAD，并设均值 10% 的下限 | `src/inv_agent/compute.py`、`tests/test_compute.py` |
| 慢查询对照 | 补齐优化前后对照（耗时/扫描行数/访问方式/慢日志） | `docs/慢查询对照.md` |
| 后端接口 | 新增 FastAPI 薄层，与 CLI 共用同一份实现（`graph.run_period`） | `src/inv_agent/api.py`、`tests/test_api.py` |
| 真实模型 | 接 DeepSeek 官方 API（OpenAI 兼容），含 20 元预算护栏与每日调用上限 | `src/inv_agent/llm.py`、`docs/LLM冒烟验证_20260924.md` |
| held-out 纪律 | 阈值调整过的场景（`DEMAND_SHIFT`）移出 held-out | `src/inv_agent/scenarios.py`、`tests/test_holdout_policy.py` |
| 数据重灌 | seed 现在连 `job_run` 一起清，重灌后不会被旧任务锁死 | `src/inv_agent/data_pipeline.py` |
| 性能对照表 | 放大表挪到独立库 `perf_lab`，业务库只保留 13 张业务表 | `scripts/perf_compare.py` |
| 桩开关 | `LLM_FORCE_STUB=1`：跑演示数据/回归时即使配了 key 也走确定性桩 | `src/inv_agent/llm.py` |
| 两个真 bug | 重灌没清图检查点导致带旧 `case_id` 恢复（外键失败）；包装倍数向上取整把库存顶过上限（差几个单位的假违规） | 见「已修 bug」一节 |

## v1.2 变更（相对 v1.1）

| 项 | 变更 | 证据位置 |
|---|---|---|
| 自然语言入口层 | 新增「自然语言 → 结构化参数」：模型只抽意图与实体名，主键一律由数据库解析，编造实体直接拒 | `src/inv_agent/intake.py` |
| 三种意图 + 判定规则 | `CHECK_STOCK` / `PLAN_REPLENISH` / `EXPLAIN_DECISION`，规则写进提示词（不是让模型凭感觉判） | 同上 |
| 相对时间 | 本周/上周/上上周/下周 相对「数据里最新周期」解析，显式 ISO 周优先 | 同上 |
| 入口 | CLI `ask --text "…" [--run]`、接口 `POST /intake` | `cli.py`、`api.py` |
| 冻结用例集 | 10 条自然语言用例（期望值我按种子数据手写）：桩 10/10、真实模型 10/10 | `src/inv_agent/nl_cases.py`、`docs/NL入口冒烟_20260926.md` |
| 三个真 bug | 见下表 #10–#12 | — |

## v1.3 变更（相对 v1.2）

| 项 | 变更 | 证据位置 |
|---|---|---|
| 连接复用 | 事务不再每次新建 MySQL 连接（按线程复用）：200 个 SKU 的批量计划 164.8 秒 → **12.70–12.74 秒**（约 13×，桩模式墙钟，3 次）；全量测试从 66 秒降到 10 秒 | `src/inv_agent/db.py`、`docs/批量与投影性能对照.md` |
| 批量并发 | `plan-period --workers N`，批量路径改用不带 checkpointer 的图（避免 SQLite 争用）：4 路并发再降到 **8.74–8.78 秒**（1.45×） | `src/inv_agent/graph.py`、`tests/test_concurrency.py`、`docs/批量与投影性能对照.md` |
| 稳健 σ 对照 | 同批需求回放：库存深度 −30%，但缺货率 +5.05 个百分点；下限 10%→25% 无差异 | `docs/稳健σ对照_20260926.md` |
| 延迟结论 | 诊断节点延迟来自输出长度（1.8k–4.9k token）；**限制 max_tokens 会把响应截成空**（400/800 均 0/3 通过），收紧提示词仅 −12% | `docs/LLM延迟对照_20260926.md` |
| **性能口径（别混引）** | 上两行是 **Docker 三服务 + 走 HTTP 批量事实** 的口径（12.70–12.74 秒）；同日换 **宿主机原生 MySQL 8.4.9（本地直连、无容器网络）** 复测为 11.37 / 7.99 / 7.99 秒。后端不同、不能横向比较 | `docs/批量与投影性能对照.md` 第七、八节 |

## v1.4 变更（相对 v1.3，第一期收口）

| 项 | 结论与数字 | 证据位置 |
|---|---|---|
| 并发实测（真实模型） | 120 个 SKU、20 个诊断：串行 297.5 秒 → 4 路并发 73.5 秒，**加速比 4.05×**，两轮均 0 错误 | `docs/并发对照_20260926.md` |
| 多 Agent 对照 | 双 Agent（证据收集+归因）覆盖族数 2.70 → 3.00、全覆盖 8/10 → 10/10，代价是**调用翻倍、token +88%、耗时翻倍**，结论一致 9/10 → 证据支持保持单 Agent | `docs/多Agent对照_20260926.md` |
| 自然语言用例 | 从 10 条扩到 **30 条**（含编造编号、无实体、多实体、歧义四类陷阱）：桩 37 条测试通过、真实模型 **30/30**，平均延迟 1.29 秒 | `src/inv_agent/nl_cases.py`、`docs/NL入口冒烟_20260926.md` |
| 稳健 σ 多种子 | 5 组扰动（库存初值 0.9–1.1 × 交期 ±2 天）下结论方向**全部一致**：缺货率 +4.87~+5.48 个百分点、库存 −28%~−33% | `docs/稳健σ对照_20260926.md` |
| 未启用能力归档 | 偏好记忆模块移出生产路径，存 `experiments/preference_memory/` 并注明为什么不接 | `experiments/preference_memory/README.md` |

## v1.5 变更（相对 v1.4，载体边界）

| 项 | 结论与数字 | 证据位置 |
|---|---|---|
| 载体服务 | 新增 `commerce-core`：独立库 `commerce` + 独立账号 `commerce_svc`；5 个接口（商品与参数、库存快照、在途、销量、收货确认）+ 交期偏差接口 | `src/commerce_core/`、`migrations/commerce/001_schema.sql` |
| 事实源适配器 | Agent 侧 `OMS_MODE=table\|http` 两种模式返回同一份行结构，**业务逻辑不改** | `src/inv_agent/facts.py` |
| 换源等价 | 全量 200 个 SKU、周期 W31：数量/金额/内容哈希 **200/200 一致** | `tests/test_commerce_boundary.py` |
| 上游不可用 | 判 `UPSTREAM_TIMEOUT`（不是 NOT_FOUND）；商品清单都拉不到时任务判 FAILED + 写审计，不留悬锁 | 同上、`docs/载体边界_20260926.md` |
| 账号隔离 | Agent 账号读载体库被拒（负向用例守着，不是口头边界） | 同上 |
| 收货确认 | 幂等键命中返回原流水、库存只加一次；收货量超在途被拒（409 `QTY_OUT_OF_RANGE`） | 同上 |
| 投影性能 | 逐行 INSERT >450 秒（未跑完）→ `executemany` **5.02–5.52 秒**（3 次，约 5.8 万行，行数 200/56000/33175/3132） | `scripts/project_to_commerce.py`、`docs/批量与投影性能对照.md` |

一期边界：载体是本地模拟的上游（数据由同一份模拟结果投影，两库同源）；复算指标仍读本地投影。
真实业务里采购单权威在上游 ERP，那是**二期（选项 2）**：幂等键跨系统传递、超时回查、定期对账。

## v1.6 变更（相对 v1.5，单价绑定）

审查发现的真 bug：建议金额按事实源单价算，执行器却读本地 `sku.unit_cost`；
演示数据同源所以测不出来，接真上游一调价就是"批的是 A 价、落的是 B 价"。

| 项 | 结论 | 证据位置 |
|---|---|---|
| 单价进哈希 | 建议金额与 `content_hash` 绑定事实源单价；单价落库到 `replenishment_suggestion.unit_price` | `src/inv_agent/pipeline.py`、`migrations/003_suggestion_unit_price.sql` |
| 审批快照 | 快照冻结单价/数量/金额，两条审批路径共用同一个冻结函数 | `src/inv_agent/guardrails.py`、`graph.py` |
| 执行器 | 只认快照里的单价与金额，不再读本地表 | `src/inv_agent/executor.py` |
| 执行前复检 | 当前事实源单价 ≠ 快照单价，或快照金额 ≠ 建议金额 → `APPROVAL_INVALIDATED` + 审计 | 同上 |
| 对照证据 | 旧口径 999.00×10 = 9990.00（与审批金额 1000.00 自相矛盾）；新口径 100.00×10 = 1000.00 一致 | `tests/test_price_binding.py`（4 条）、`docs/载体边界_20260926.md` 第七节 |

## v1.7 变更（相对 v1.6，批量事实与数据版本）

按裁定只加两项：批量取数 + 数据版本。**没有**上游建单、库存调整、退货、对账、应付，也没有新增状态机。

| 项 | 结论与数字 | 证据位置 |
|---|---|---|
| 批量事实接口 | `GET /facts`：一次传 SKU 列表，返回商品/库存/在途/销量/交期偏差；内部固定 5 条批量查询（窗口函数取最新一条/最近 N 条），单次上限 200 个 SKU | `src/commerce_core/api.py`、`repository.facts_batch` |
| 调用次数 | 全量 200 个 SKU：逐个取数 **1000 次调用 / 8.1 秒** → 批量 **1 次 / 0.8 秒**（**10.0×**）；计数进了 `job_run.stats_json.source_calls` | `docs/批量事实与数据版本_20260926.md` |
| 换批量不改结论 | 同一批 20 个 SKU，逐个 vs 批量：数量/金额/内容哈希完全一致 | `tests/test_facts_batch.py` |
| 数据版本 | 商品/在途带 `data_version`、库存带 `version`，三者都有 `updated_at`；在途版本按 SKU 单独取（收货后不会"消失"） | `migrations/commerce/002_data_version.sql` |
| 判断新旧 | `If-None-Match` → 304 复用缓存；`updated_since` → 只回变过的 SKU；上游收货后版本与整批 ETag 都变 | 同上 |
| Agent 侧 | `prefetch()` 按请求指纹缓存（TTL + ETag），未命中的 SKU 自动回退逐 SKU 接口；建议的 `basis` 写入事实版本 | `src/inv_agent/facts.py`、`pipeline.py` |
