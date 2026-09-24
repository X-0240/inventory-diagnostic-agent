# 库存补货与调拨 Agent（边界内诊断）

用 LLM 做边界内诊断，用规则守数量，用审计证明边界。

**本项目不主张"AI 会补货"**：补货数量由确定性公式给出（安全库存、(s,S)、经济订货批量），
LLM 只在异常 SKU 上做假设生成、证据收集与整理、是否需要人工介入的判断。

## v1.1 变更（相对 v1）

| 项 | 变更 | 证据位置 |
|---|---|---|
| 离散度估计 | 改用**稳健 σ**：间歇需求用"发生-规模"模型，其余用 MAD，并设均值 10% 的下限 | `src/inv_agent/compute.py`、`tests/test_compute.py` |
| 慢查询对照 | 补齐优化前后对照（耗时/扫描行数/访问方式/慢日志） | `docs/慢查询对照_20260924.md` |
| 后端接口 | 新增 FastAPI 薄层，与 CLI 共用同一份实现（`graph.run_period`） | `src/inv_agent/api.py`、`tests/test_api.py` |
| 真实模型 | 接 DeepSeek 官方 API（OpenAI 兼容），含 20 元预算护栏与每日调用上限 | `src/inv_agent/llm.py`、`docs/LLM冒烟验证_20260924.md` |
| held-out 纪律 | 阈值调整过的场景（`DEMAND_SHIFT`）移出 held-out | `src/inv_agent/scenarios.py`、`tests/test_holdout_policy.py` |
| 数据重灌 | seed 现在连 `job_run` 一起清，重灌后不会被旧任务锁死 | `src/inv_agent/data_pipeline.py` |

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
- v1.1 后：`pytest tests -q` → **66 passed**（新增接口层、稳健离散度、held-out 纪律用例）。
- 慢查询对照（放大到 150 万行、同一批数据只改索引）：
  Q1 聚合 489.7 ms → 80.4 ms（扫描 150 万 → 4.5 万行，优化前 3 条进慢日志、优化后 0 条）；
  Q2 单 SKU 明细 281.5 ms → 1.1 ms（扫描 150 万 → 50 行，filesort 消失）；
  Q3 审核队列分页 51.9 ms → 2.6 ms（扫描 20 万 → 20 行）。
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

## 相关文档

- 方案与接口契约：`docs/方案草案_20260924.md`（第 10 节已冻结）
- 项目指引与进度：`AGENTS.md`
