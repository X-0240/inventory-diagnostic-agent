"""统一配置入口：所有环境变量只在这里读，避免各处散读导致口径漂移。"""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT=Path(__file__).resolve().parents[2]
load_dotenv(ROOT/".env")

#数据库
DB_HOST=os.getenv("DB_HOST","127.0.0.1")
DB_PORT=int(os.getenv("MYSQL_PORT","3306"))
DB_NAME=os.getenv("MYSQL_DATABASE","inventory")
DB_USER=os.getenv("MYSQL_APP_USER","inv_app")
DB_PASSWORD=os.getenv("MYSQL_APP_PASSWORD","")

#载体（上游事实源）：table=直读本地库（默认），http=调 commerce-core 载体服务
OMS_MODE=os.getenv("OMS_MODE","table")
COMMERCE_BASE_URL=os.getenv("COMMERCE_BASE_URL","http://127.0.0.1:8001")
COMMERCE_API_TOKEN=os.getenv("COMMERCE_API_TOKEN","local-commerce-token")
#载体超时判成 UPSTREAM_TIMEOUT（不可用≠没数据）；重试与否由编排层策略决定
COMMERCE_TIMEOUT_SECONDS=float(os.getenv("COMMERCE_TIMEOUT_SECONDS","5"))

#数据生成参数（契约 7：对照实验前冻结并预注册）
GEN_SEED=int(os.getenv("GEN_SEED","20260924"))
GEN_SKU_LIMIT=int(os.getenv("GEN_SKU_LIMIT","200"))
GEN_START_DATE=os.getenv("GEN_START_DATE","2011-01-01")
GEN_WEEKS=int(os.getenv("GEN_WEEKS","40"))

#LLM：没配 key 时用 Stub，保证离线可跑、测试可复现
LLM_BASE_URL=os.getenv("LLM_BASE_URL","")
LLM_API_KEY=os.getenv("LLM_API_KEY","")
LLM_MODEL=os.getenv("LLM_MODEL","")
#成本护栏：每天最多调用多少次真实模型（防重试/循环烧钱）
LLM_DAILY_CALL_BUDGET=int(os.getenv("LLM_DAILY_CALL_BUDGET","50"))
LLM_TIMEOUT_SECONDS=int(os.getenv("LLM_TIMEOUT_SECONDS","60"))
#输出上限：防模型跑飞（实测未加限时输出过 2.4k–4.2k token，延迟 15 秒以上）
#注意：默认 0＝不限制。实测这个模型输出 1.8k–4.9k token，任何上限都会把响应截成空字符串，
#限制输出不能用来提速（详见 docs/LLM延迟对照_20260926.md）。
LLM_MAX_TOKENS=int(os.getenv("LLM_MAX_TOKENS","0"))
#成本护栏：累计金额上限（元）与单价（元/百万 token，设定值，需按官方价目核对）
LLM_BUDGET_CNY=float(os.getenv("LLM_BUDGET_CNY","20"))
LLM_PRICE_IN_CNY_PER_1M=float(os.getenv("LLM_PRICE_IN_CNY_PER_1M","2.0"))
LLM_PRICE_OUT_CNY_PER_1M=float(os.getenv("LLM_PRICE_OUT_CNY_PER_1M","8.0"))

#计算层参数（设定值，非行业实测，写进 config_version 冻结）
RULE_VERSION=os.getenv("RULE_VERSION","v1")
BASELINE_WINDOW_DAYS=int(os.getenv("BASELINE_WINDOW_DAYS","28"))
REVIEW_PERIOD_DAYS=int(os.getenv("REVIEW_PERIOD_DAYS","7"))
ORDER_COST=float(os.getenv("ORDER_COST","80"))
HOLDING_RATE=float(os.getenv("HOLDING_RATE","0.22"))
INVENTORY_CAP_MULTIPLIER=float(os.getenv("INVENTORY_CAP_MULTIPLIER","2.0"))

#异常分界（设定值，非行业实测）
ANOMALY_ENTER_SCORE=float(os.getenv("ANOMALY_ENTER_SCORE","3.0"))
ANOMALY_EXIT_SCORE=float(os.getenv("ANOMALY_EXIT_SCORE","2.0"))
ANOMALY_CONFIRM_DAYS=int(os.getenv("ANOMALY_CONFIRM_DAYS","2"))
ANOMALY_DAILY_QUOTA=int(os.getenv("ANOMALY_DAILY_QUOTA","20"))

#审批金额阈值（设定值）
APPROVER_AMOUNT_LIMIT=float(os.getenv("APPROVER_AMOUNT_LIMIT","5000"))

WAREHOUSE_ID=os.getenv("WAREHOUSE_ID","WH1")
DATA_DIR=ROOT/"data"
RAW_DIR=DATA_DIR/"raw"
