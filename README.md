# 库存补货与调拨 Agent（边界内诊断）

用 LLM 做边界内诊断，用规则守数量，用审计证明边界。

**本项目不主张"AI 会补货"**：补货数量由确定性公式给出（安全库存、(s,S)、经济订货批量），
LLM 只在异常 SKU 上做假设生成、证据收集与整理、是否需要人工介入的判断。

## 当前状态（第一期已收口）

| 项 | 值 |
|---|---|
| 冻结版本 | **v1.7**（提交 `f62a540`，工作树干净） |
| 回归测试 | `PYTHONPATH=src .venv/Scripts/python -m pytest tests -q` → **123 passed**（v1.7 收口 121 条 + 提交前审查卫生用例 2 条） |
| 建库 | `scripts/migrate.py`（业务库 3 个迁移）、`scripts/migrate_commerce.py`（载体库 2 个迁移） |
| 演示数据 | W20 141 条建议、W21 154 条建议；载体库已投影（商品 200 / 库存 56000 / 销量 33175 / 在途 3132） |
| 二期或以后 | `seed` 改批量提交（只影响重灌效率）、跨系统写（采购单权威在上游）、复算走聚合接口、共享缓存 |

外部审查的收口结论：不改正确性、只影响开发效率的项留到以后，当前版本收口。

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

## 快速开始

```bash
#1 起 MySQL（Docker Desktop 需先启动）
docker compose up -d

#2 装依赖
python -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt

#3 建表
PYTHONPATH=src .venv/Scripts/python scripts/migrate.py

#4 下载公开销量并构造环境（约 2 分钟）
PYTHONPATH=src .venv/Scripts/python -m inv_agent.cli seed

#5 跑一个周期的计划（连续两个周期后异常才会被确认进入诊断）
PYTHONPATH=src .venv/Scripts/python -m inv_agent.cli plan-period --period 2011-W20
PYTHONPATH=src .venv/Scripts/python -m inv_agent.cli plan-period --period 2011-W21

#6 看建议 → 审批 → 执行 → 复算
PYTHONPATH=src .venv/Scripts/python -m inv_agent.cli list --status PENDING_APPROVAL
PYTHONPATH=src .venv/Scripts/python -m inv_agent.cli approve --suggestion 151 --decision APPROVE --actor bob --role SUPERVISOR
PYTHONPATH=src .venv/Scripts/python -m inv_agent.cli report --period 2011-W21

#7 崩溃恢复
PYTHONPATH=src .venv/Scripts/python -m inv_agent.cli recover

#8 测试
PYTHONPATH=src .venv/Scripts/python -m pytest tests -q

#9 载体服务（可选：走 "Agent 只读上游" 的模式）
.venv/Scripts/python scripts/migrate_commerce.py      # 建载体库与独立账号（幂等）
.venv/Scripts/python scripts/project_to_commerce.py   # 把本地生成的事实投影进载体库
PYTHONPATH=src .venv/Scripts/python -m uvicorn commerce_core.api:app --port 8001
OMS_MODE=http PYTHONPATH=src .venv/Scripts/python -m inv_agent.cli plan-period --period 2011-W31 --force

#9 接口层（另开终端；接口用请求头 X-Actor / X-Role 传身份，只做权限矩阵校验）
PYTHONPATH=src .venv/Scripts/python -m uvicorn inv_agent.api:app --port 8100

#10 慢查询对照（会造两张放大表，约 1 分钟）
PYTHONPATH=src .venv/Scripts/python scripts/perf_compare.py

#11 真实模型冒烟（需先配好 .env 的 LLM 三项）
PYTHONPATH=src .venv/Scripts/python scripts/llm_smoke.py
```

## 主链

销量与库存数据 → Agent 编排基线需求估算工具 → 结合在途、安全库存、交期做多步分析 →
生成补货建议（含金额与依据）→ 护栏校验（金额上限、供应商账期、库存上限、供应商额度）→
人工审批（LangGraph `interrupt`）→ 受控执行器写入采购单 → 复算指标。

## 代码结构

| 路径 | 职责 |
|---|---|
| `src/inv_agent/compute.py` | 确定性计算层（安全库存、再订货点、(s,S)、EOQ），纯函数 |
| `src/inv_agent/anomaly.py` | 异常分界（五项分数、双阈值滞回、连续确认、每日配额） |
| `src/inv_agent/guardrails.py` | 写操作护栏与审批快照复检 |
| `src/inv_agent/llm.py` | LLM 客户端：无 key 时用确定性桩，有 key 走 OpenAI 兼容接口 |
| `src/inv_agent/diagnosis.py` | 受限动作空间与 schema 校验（枚举外一律拒绝） |
| `src/inv_agent/pipeline.py` | 唯一一份评估实现（LangGraph 节点与批量扫描共用） |
| `src/inv_agent/graph.py` | LangGraph 编排（含 `interrupt` 审批与续跑） |
| `src/inv_agent/repository.py` | 数据访问（条件更新、幂等、审计写入） |
| `src/inv_agent/executor.py` | 受控执行器（唯一有写采购单权限的模块） |
| `src/inv_agent/metrics.py` | 复算（缺货率、周转天数、超储金额） |
| `src/inv_agent/data_pipeline.py` | 数据管线：公开销量 + 模拟库存/在途/交期/账期 |
| `migrations/` | 13 张表的权威 schema + 审计不可变触发器 |
| `tests/` | 计算、异常、护栏、诊断、数据库约束、执行器、场景集 |

## 数据来源与口径

- 销量：UCI Online Retail（CC BY 4.0，公开数据集），**仅作需求信号**，`source_flag='PUBLIC'`。
- 库存、在途、交期、账期：由 `data_pipeline.py` 按销量推导生成，参数与种子写入 `config_version`
  与 `data/frozen_params.json`（预注册，可复现）。
- 场景扰动（促销突增、需求上移、新品无历史、供应商延迟、缺货级联、数据断点）**落在数据层**
  （`sales_daily`、`inbound_order`），不是模拟器内存里的临时改动。
- 所有业务效果数字都标注为**仿真 KPI**，不声称真实业务水平。

## 已实测的证据（2026-09-24）

- `pytest tests -q` → **48 passed**（含数据库约束与执行器用例）。
- **当前（v1.7）：`pytest tests -q` → 123 passed**；历史上 v1.1 后为 64 passed、第一期开工时为 48 passed。
- 慢查询对照（放大到 150 万行、同一批数据只改索引）：
  Q1 区间聚合 **508–515 ms → 145–159 ms**（扫描 150 万 → 9 万行，优化前 3 条进慢日志、优化后 0 条）；
  Q2 单 SKU 明细 **251–257 ms → 1.25–1.28 ms**（扫描 150 万 → 50 行，filesort 消失）；
  Q3 审核队列分页 **50 ms → 2.4–3.6 ms**（扫描 20 万 → 20 行）。
  **口径说明**：放大表每次运行重建，同一查询跨次波动约 ±10%，所以写成多次独立测量的区间而不是单点值；
  四次测量（09-24、09-27 各两次）结论一致，完整记录以 `docs/慢查询对照.md` 为准，历史版本看 git 历史。
历史曾记录 Q1 优化后 80.4 ms，多次重测均落在 145–159 ms，**该值不可复现，已弃用**。
- 真实模型冒烟（每场景 2 个 held-out SKU，共 12 次调用）：schema 通过 12 / 拒绝 0，
  平均延迟 18.5 秒，本次估算成本 0.3972 元（上限 20 元）。
- 故障注入：采购单已写入、建议未置 `EXECUTED` 时崩溃 → `recover` 后建议回填为 `EXECUTED`，
  且**该建议名下的采购单仍然只有 1 张**（`tests/test_db_and_executor.py`）。
- 重复执行同一建议 → `STATE_CONFLICT`，不产生第二张采购单。
- 审计表在库层禁止 UPDATE/DELETE（触发器强制）。
- 同 SKU 同周期只允许一条活跃建议（生成列 + 唯一索引强制）。
- 场景数据规模：200 个 SKU、33,175 行销量、56,000 条库存快照、2,956 张模拟采购单
  （200 SKU × 280 天）。
- 仿真 KPI 示例（W22）：缺货率 22.03%（156/708 有需求的 SKU-天）；超储金额 1,913,258.99。

## 已修 bug（都是自己压出来的，不是等报障）

| # | 现象 | 根因 | 修法 |
|---|---|---|---|
| 1 | 缺货率虚高到 74% | 模拟器先剔到期货再算到货量，货永远到不了 | 先结算到货再移出在途 |
| 2 | 批发型 SKU 欠库存 | `target_qty` 只算单周期需求，没算保护期波动 | 保护期 = 交期 + 复核周期，含波动缓冲 |
| 3 | EOQ 放大时必撞库存上限 | 上限被当成"下单量上限"而不是"下单后库存水平上限" | 改成约束库存水平 |
| 4 | 周期性批量计划整轮崩 | 诊断信号字段名与 facts 不一致（两处） | 对齐字段并用 `.get()` 兜底 |
| 5 | 审批写库报 Decimal 不能序列化 | 数据库数值列是 Decimal | 统一 JSON 兜底转换 |
| 6 | 场景异常在数据里不可见 | 扰动只写在模拟器内存，没落到 `sales_daily` | 扰动改在数据层生效 |
| 7 | 一次崩溃把周期永久锁死 | `job_lock` 没有过期机制 | 加超时与 `--force` |
| 8 | 重灌后批量计划报外键错误 | 重灌没清图检查点，旧 checkpoint 带着已删的 `case_id` 恢复 | 重灌清 checkpoint + 批量模式每次用新 thread + 落库前校验 case |
| 9 | 40% 建议被推成人工接管 | 包装倍数向上取整把库存顶过上限（上限 2324、实际 2325） | 受上限约束时按包装倍数向下取整 |
| 10 | 归因结论进了案件表却没进建议单 | LangGraph 状态通道只跟踪**已声明**的键，`RunState` 漏了 `diagnosis`，节点返回的结论被静默丢弃 | 补声明该键 + 加走图的落库回归测试 |
| 11 | 已存在的建议单不会被补写归因 | 幂等重放直接返回旧行 | 新增"只补不覆盖"的 `enrich_diagnosis`（仅当 `hypothesis_type` 为空时写入） |
| 12 | 入口层一开始判 `SCHEMA_INVALID` | 误用了**诊断节点**的客户端（提示词与 schema 都不同） | 入口层独立 `get_parser()` |

## 已知限制（诚实清单）

1. **没有 LLM key 时诊断走确定性桩**。桩的判据是启发式，所以本项目**不声称分类准确率**；
   场景测试只断言"设计信号在数据里可见"和"诊断输出合法"，分类分布只作报告。
2. **批发型 SKU 的库存深度已用稳健 σ 缓解，但未做前后数字对照**：v1.1 把 σ 换成
   MAD + 间歇需求模型 + 均值下限，周转天数仍同时给均值与中位数（`turnover_days_median`）；
   "稳健 σ 到底改善了多少"需要在同一批场景下跑前后对照才能给数字，目前没有。
3. **真实模型延迟偏高**：冒烟实测平均 18.5 秒/次（12 次调用）。按"只对异常 SKU 调用"的设计，
   一个周期 20 个异常 SKU 意味着约 6 分钟，需要调超时/并发或换更快的模型，v1.1 未处理。
3. **缺货率约 22%** 是仿真口径（有需求且库存为 0 的 SKU-天 / 有需求的 SKU-天），
   不等于真实业务水平。
4. **采购单不覆盖部分到货与退货**：终态只到 `RECEIVED/CLOSED`，多批次收货与反向退货属第二期。
5. 场景测试里的 `DEMAND_SHIFT` 可见性阈值是**观察后调整**的（实测 6/12，稀疏销量 SKU 的
   1.6 倍水平变化落在噪声里），已在测试文件中注明，不作为识别率结论。
6. 演示数据（W20/W21）用的是**确定性桩**（`LLM_FORCE_STUB=1`），不是真实模型；
   真实模型只在 `docs/LLM冒烟验证_20260924.md` 里跑过 12 次。
7. 自然语言用例集只有 **10 条**，期望值由我手写，其中两条意图标注**在我观察到模型答案后修正过**
   （"够不够卖"归 `CHECK_STOCK`、"处理结果"归 `EXPLAIN_DECISION`，依据是写进提示词的判定规则）；
   报的是功能用例通过率，不是模型能力评测。
8. **诊断节点的真实模型延迟没有根治**：实测结论是"只能从调用侧解决"（并发或换模型），
   并发收益在桩模式下只有 1.45×（没有模型等待可重叠）；真实模型下已实测 **4.05×**
   （120 个 SKU / 20 个诊断，297.5 秒 → 73.5 秒，见 `docs/并发对照_20260926.md`）。
9. 稳健 σ 的对照是**单次确定性回放**（需求用历史真实值，不重复抽样），没有多种子；结论只对这组数据成立。

## 相关文档

- 方案与接口契约：`docs/方案草案_20260924.md`（第 10 节已冻结）
- 项目指引与进度：`AGENTS.md`
