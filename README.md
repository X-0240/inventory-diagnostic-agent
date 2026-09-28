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

版本演进（每一版改了什么、为什么改）见 [CHANGELOG.md](CHANGELOG.md)；实测证据、已修 bug 与已知限制见 [docs/证据与局限.md](docs/证据与局限.md)。

## 快速开始

```bash
#1 起 MySQL（Docker Desktop 需先启动）
docker compose up -d
#  若本机 Docker 不可用：装一个本机 MySQL 8.4，按 .env 建好库与账号，
#  再用 `mysql -e "source <migrations/*.sql>"` 手工建表（migrate.py 走的是 docker exec）

#2 装依赖
python -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt

#3 建表
PYTHONPATH=src .venv/Scripts/python scripts/migrate.py

#4 下载公开销量并构造环境（本机实测约 6 分钟：逐行写入，见 CHANGELOG 里的二期项）
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

## 文档入口

- 版本演进：[`CHANGELOG.md`](CHANGELOG.md)
- 实测证据 / 已修 bug / 已知限制：[`docs/证据与局限.md`](docs/证据与局限.md)
- 方案与接口契约：[`docs/方案草案_20260924.md`](docs/方案草案_20260924.md)（第 10 节已冻结）
- 各专项对照（慢查询、并发、多 Agent、稳健 σ、载体边界、批量事实、LLM 延迟、NL 入口、批量与投影性能）：`docs/`
- 项目内部指引、部署事实与当前进度记录在本地 `AGENTS.md`（不进公开仓库）

## 协作与反馈

个人项目，没有开放的贡献流程；发现问题、有建议或想指出口径错误，请开
[Issue](https://github.com/X-0240/inventory-diagnostic-agent/issues)。

## 许可与数据来源

代码与文档采用 [MIT 许可](LICENSE)。数据部分单独说明：

- **销量数据**来自 UCI Machine Learning Repository 的 Online Retail 数据集（CC BY 4.0），仅作需求信号使用，署名与来源见上文「数据来源与口径」。该数据**不适用**本仓库的 MIT 许可，遵循其原始许可。
- **库存、在途、交期、账期**由本地模拟器按固定随机种子生成，属**构造数据**：本仓库不对其真实性作任何主张，也不代表真实商业数据。
- 文档里的性能与指标数字都是本机实测值，随硬件、数据库后端与数据状态变化；两种数据库后端（Docker MySQL / 本机 MySQL）的口径已在 [`docs/证据与局限.md`](docs/证据与局限.md) 与 `docs/批量与投影性能对照.md` 分开记录，不能混引。
