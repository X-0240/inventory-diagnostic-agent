# 库存补货与调拨 Agent（边界内诊断）

用 LLM 做边界内诊断，用规则守数量，用审计证明边界。

**本项目不主张"AI 会补货"**：补货数量由确定性公式给出（安全库存、(s,S)、经济订货批量），
LLM 只在异常 SKU 上做假设生成、证据收集与整理、是否需要人工介入的判断。

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
2. **少数高销量批发型 SKU 的库存深度不合理**：公开数据是批发订单，σ 被极端值抬高，
   导致周转天数被离群值带偏（W22 均值 589 天，因此补了中位数口径 `turnover_days_median`）。
   下一版计划用稳健离散度估计（如 MAD）或对数量做 winsorize。
3. **缺货率约 22%** 是仿真口径（有需求且库存为 0 的 SKU-天 / 有需求的 SKU-天），
   不等于真实业务水平。
4. **采购单不覆盖部分到货与退货**：终态只到 `RECEIVED/CLOSED`，多批次收货与反向退货属第二期。
5. 场景测试里的 `DEMAND_SHIFT` 可见性阈值是**观察后调整**的（实测 6/12，稀疏销量 SKU 的
   1.6 倍水平变化落在噪声里），已在测试文件中注明，不作为识别率结论。

## 相关文档

- 方案与接口契约：`docs/方案草案_20260924.md`（第 10 节已冻结）
- 项目指引与进度：`AGENTS.md`
