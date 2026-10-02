# 库存补货与调拨 Agent（边界内诊断）

把「销量 / 库存 / 在途」变成**可审批的补货建议**：数量由确定性公式算出，LLM 只在异常 SKU 上做归因与解释，
所有写操作都必须经过 **护栏 → 人工审批 → 受控执行器 → 复算与审计**。

## 这个项目解决什么问题

- **补货量必须可复现、可审计**。同一批数据跑两次，数量必须一样；所以数量只由公式给出（安全库存、(s,S)、经济订货批量），
  不交给模型。模型输出与公式不一致时以公式为准，并写进审计。
- **异常 SKU 需要人看得懂的解释**。促销、新品、供应商延迟、销量突变这些情况，"数字对不上"的原因不止一个，
  需要多步归因：给假设 + 引用具体证据，而不是给一句结论。
- **写采购单有真实代价**。金额上限、供应商账期、库存上限、供应商额度都是**写操作的前置护栏**；
  模型没有业务库写权限，唯一能写 `purchase_order` 的模块是受控执行器。
- **审批之后不能重复下单、状态不能被并发改坏**。方案内容哈希绑定审批、幂等键防重复提交、
  条件状态更新防竞争、执行前复检审批快照。

## 跑一个完整周期（示例）

```bash
docker compose up -d                                          # 起 MySQL 8.4
PYTHONPATH=src .venv/Scripts/python scripts/migrate.py         # 建表（13 张）
PYTHONPATH=src .venv/Scripts/python -m inv_agent.cli seed      # 灌演示数据：公开销量 + 构造库存/在途
PYTHONPATH=src .venv/Scripts/python -m inv_agent.cli plan-period --period 2011-W20
PYTHONPATH=src .venv/Scripts/python -m inv_agent.cli plan-period --period 2011-W21
PYTHONPATH=src .venv/Scripts/python -m inv_agent.cli list --status PENDING_APPROVAL
PYTHONPATH=src .venv/Scripts/python -m inv_agent.cli approve --suggestion <id> --decision APPROVE --actor bob --role SUPERVISOR
PYTHONPATH=src .venv/Scripts/python -m inv_agent.cli report --period 2011-W21
```

**跑完会看到**（口径：桩模式、演示数据、单机 MySQL）：

| 预期输出 | 值 |
|---|---|
| W20 / W21 生成的建议 | 141 条 / 154 条 |
| 进入归因节点的 SKU | 16 个（"连续两周期确认"才进） |
| 人工接管 / 错误 | 0 / 0 |

`list` 打印待审批建议（SKU、周期、数量、金额、状态）；`approve` 之后执行器才会写采购单；
`report` 复算缺货率、周转天数、超储金额。这些是**演示数据的期望结果，不是模型能力评测**。

## 快速开始

| 依赖 | 版本 | 说明 |
|---|---|---|
| Python | 3.12 | `Dockerfile` 用 `python:3.12-slim` |
| MySQL | 8.4 | `docker compose up -d` 起；也可用本机 MySQL（见下） |
| 依赖包 | 见 `requirements.txt` | langgraph、pymysql、fastapi、pandas、pydantic 等 |

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt
cp .env.example .env          # 填数据库密码；LLM_* 留空即走确定性桩
```

**不想烧模型钱、也不想每次等十几秒**：设 `LLM_FORCE_STUB=1`（即使配了 key 也走桩，演示与回归测试都用它）。

**用本机 MySQL 而不是 Docker**：装一个 MySQL 8.4，按 `.env` 建好库与账号，再用
`mysql -e "source migrations/001_schema.sql"` 逐个执行迁移（`scripts/migrate.py` 走的是 `docker exec`）。

## 架构

### 1）主链（一个周期怎么跑完）

```
批量路径（一个周期，默认 200 个 SKU）
  商品清单(事实源 facts) → 逐 SKU 计算(compute：基线/安全库存/(s,S)/EOQ)
      → 候选筛选(anomaly：五项异常分 + 双阈值滞回 + 每日配额)
      → 连续两周期确认 → 异常 SKU 进归因(diagnosis：受限动作空间 + schema 校验)
      → 建议落库(pipeline) → 护栏(guardrails：金额/账期/库存上限/供应商额度)
      → 人工审批(LangGraph interrupt，跨进程等待)
      → 受控执行器(executor：全系统唯一写 purchase_order 的模块)
      → 复算(metrics：缺货率/周转天数/超储金额) → 审计(audit_event)
```

对应 LangGraph 单 SKU 图的节点（`src/inv_agent/graph.py`）：

```
START → load → [skip | diagnose] → persist → approval → record_approval → [execute | finish] → recompute → finish → END
```

- 正常 SKU 走 `skip`，不进模型；只有异常 SKU 才走 `diagnose`——模型成本与延迟都压在这条支线上。
- `approval` 是 `interrupt()`：审批人几分钟后或第二天再决定都行，进程重启也能续跑（checkpointer 记轨迹）。
- 批量路径编译成**不带 checkpointer 的图**（业务状态才是权威源，批量不需要续跑，还能避开 SQLite 争用）。

### 2）模块职责

| 模块 | 职责 | 关键约束 |
|---|---|---|
| `compute.py` | 确定性计算层：基线需求估算、安全库存、(s,S)、经济订货批量 | 数量只从这里出 |
| `anomaly.py` | 异常分界：五项异常分 + 双阈值滞回 + 连续确认 + 每日配额 | 滞回防抖动、配额防一天打爆预算 |
| `diagnosis.py` | 异常归因：受限动作空间 + schema 校验 | 枚举外输出一律拒绝（`SCHEMA_INVALID`） |
| `guardrails.py` | 写操作护栏：金额上限、供应商账期、库存上限、供应商额度 | 确定性判定；规则能唯一归因的不走模型 |
| `executor.py` | 受控执行器 | **全系统唯一有权写 `purchase_order`**；只认审批快照 + 执行前复检 |
| `metrics.py` | 复算：缺货率、周转天数、超储金额 | 标注为仿真 KPI |
| `graph.py` | LangGraph 编排：节点、条件边、审批中断、恢复 | 批量与审批两条编译路径 |
| `pipeline.py` | 计划层：把单 SKU 单周期的「事实→基线→异常→建议→护栏」串起来 | |
| `facts.py` | 事实源适配器：`OMS_MODE=table` 直读本地库 / `http` 调载体接口 | 换数据源不改业务逻辑 |
| `intake.py` | 自然语言入口：意图 + 实体解析 + 幻觉拒绝 + 相对时间 | 主键由数据库解析，编造编号直接拒 |
| `repository.py` | 数据访问层 | SQL 集中在此，业务逻辑不拼 SQL |
| `db.py` | 连接与事务辅助 | 连接按线程复用（批量 164.8 秒 → 12.7 秒） |
| `errors.py` | 错误码表（16 个） | 每个都带「副作用状态 + 对账动作」 |
| `commerce_core/` | 载体服务：扮演上游事实源（独立库 + 独立账号） | Agent 账号对它零权限，有负向测试守着 |

### 3）数据模型（13 张表）

| 分组 | 表 | 说明 |
|---|---|---|
| 主数据 | `sku`、`supplier`、`policy_snapshot`、`config_version` | 商品与供应商；每个 SKU 的补货参数快照；参数版本 |
| 事实 | `sales_daily`、`inventory_snapshot`、`inbound_order` | 每日销量、每日库存快照、在途与到货 |
| 决策与流转 | `replenishment_suggestion`、`approval_record`、`purchase_order`、`exception_case` | 建议、审批、采购单、异常案件 |
| 审计与任务 | `audit_event`、`job_run` | 审计事件（触发器强制不可改）、任务统计与锁 |

### 4）四条不能越过的线

1. **数量不交给模型**：只来自 `suggest_replenishment` 工具；模型输出与工具不一致时以工具为准并记审计。
2. **写权限单点**：模型没有业务库写权限，写采购单只能经受控执行器，且只认审批快照。
3. **审批绑定内容**：方案内容哈希进审批，方案改了旧批准自动失效；执行前复检参数与单价。
4. **审计不可篡改**：库层触发器拦 `UPDATE` / `DELETE`，测试里断言修改会报错。

## 配置（`.env`，关键项节选）

| 分组 | 键 | 作用 |
|---|---|---|
| 数据库 | `MYSQL_DATABASE`、`MYSQL_APP_USER`、`MYSQL_PORT` | 库名、应用账号、端口（默认 3306） |
| 事实源 | `OMS_MODE`、`COMMERCE_BASE_URL`、`COMMERCE_TIMEOUT_SECONDS` | `table` 直读本地库 / `http` 调载体接口 |
| 数据生成 | `GEN_SEED`、`GEN_SKU_LIMIT`、`GEN_WEEKS` | 演示数据的随机种子与规模（默认 200 个 SKU） |
| 算法 | `BASELINE_WINDOW_DAYS`、`REVIEW_PERIOD_DAYS`、`ORDER_COST`、`HOLDING_RATE`、`INVENTORY_CAP_MULTIPLIER` | 基线窗口、复核周期、订货/持有成本、库存上限倍数 |
| 异常分界 | `ANOMALY_ENTER_SCORE`、`ANOMALY_EXIT_SCORE`、`ANOMALY_CONFIRM_DAYS`、`ANOMALY_DAILY_QUOTA` | 双阈值滞回、连续确认天数、每日配额 |
| 人机边界 | `APPROVER_AMOUNT_LIMIT` | 超过即需更高权限角色审批 |
| 模型 | `LLM_BASE_URL`、`LLM_MODEL`、`LLM_FORCE_STUB`、`LLM_BUDGET_CNY`、`LLM_DAILY_CALL_BUDGET` | 接口与模型名、强制走桩、预算护栏 |

## 接口

**CLI**（与 HTTP 共用同一份业务实现）：`seed`、`plan-period`（`--period` / `--workers` / `--force`）、
`list`（`--status` / `--period`）、`approve`（`--suggestion` / `--decision` / `--actor` / `--role`）、
`execute`、`ask`、`report`、`recover`、`graph-run` / `graph-resume`。

**HTTP**（FastAPI 薄层，9 个端点）：`GET /health`、`GET /suggestions`、`GET /suggestions/{id}`、
`POST /suggestions/{id}/approve`、`POST /suggestions/{id}/execute`、`POST /jobs/plan`、
`GET /metrics/{period}`、`POST /admin/recover`、`POST /intake`。

```bash
PYTHONPATH=src .venv/Scripts/python -m uvicorn inv_agent.api:app --port 8100    # 打开 http://127.0.0.1:8100/docs
PYTHONPATH=src .venv/Scripts/python -m uvicorn commerce_core.api:app --port 8001 # 载体服务
```

**诚实的边界**：接口层用请求头 `X-Actor` / `X-Role` 传身份，只做权限矩阵校验，
**没有真正的认证与令牌体系**；接入真实系统时必须替换成认证中间件。

## 文档入口

- 版本演进（每一版改了什么、为什么改）：[`CHANGELOG.md`](CHANGELOG.md)
- 实测证据 / 已修 bug / 已知限制：[`docs/证据与局限.md`](docs/证据与局限.md)
- 各专项对照（慢查询、并发、多 Agent、稳健 σ、载体边界、批量事实、LLM 延迟、NL 入口、批量与投影性能）：`docs/`
- 方案与接口契约（13 张表逐列定义、状态迁移、工具签名）：[`docs/方案草案_20260924.md`](docs/方案草案_20260924.md)

## 协作与反馈

个人项目，没有开放的贡献流程；发现问题、有建议或想指出口径错误，请开
[Issue](https://github.com/X-0240/inventory-diagnostic-agent/issues)。

## 许可与数据来源

代码与文档采用 [MIT 许可](LICENSE)。数据部分单独说明：

- **销量数据**来自 UCI Machine Learning Repository 的 Online Retail 数据集（CC BY 4.0），仅作需求信号使用。该数据**不适用**本仓库的 MIT 许可，遵循其原始许可。
- **库存、在途、交期、账期**由本地模拟器按固定随机种子生成，属**构造数据**：本仓库不对其真实性作任何主张，也不代表真实商业数据。
- 文档里的性能与指标数字都是本机实测值，随硬件、数据库后端与数据状态变化；两种数据库后端（Docker MySQL / 本机 MySQL）的口径已在 [`docs/证据与局限.md`](docs/证据与局限.md) 与 [`docs/批量与投影性能对照.md`](docs/批量与投影性能对照.md) 分开记录，不能混引。
