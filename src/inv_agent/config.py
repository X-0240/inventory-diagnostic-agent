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

#数据生成参数（契约 7：对照实验前冻结并预注册）
GEN_SEED=int(os.getenv("GEN_SEED","20260924"))
GEN_SKU_LIMIT=int(os.getenv("GEN_SKU_LIMIT","200"))
GEN_START_DATE=os.getenv("GEN_START_DATE","2011-01-01")
GEN_WEEKS=int(os.getenv("GEN_WEEKS","40"))

#LLM：没配 key 时用 Stub，保证离线可跑、测试可复现
LLM_BASE_URL=os.getenv("LLM_BASE_URL","")
LLM_API_KEY=os.getenv("LLM_API_KEY","")
LLM_MODEL=os.getenv("LLM_MODEL","")

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
