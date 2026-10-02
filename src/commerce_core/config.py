"""载体服务配置：独立库 commerce，账号与 Agent 的 inv_app 分开。"""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("MYSQL_PORT", "3306"))
DB_NAME = os.getenv("COMMERCE_DB_NAME", "commerce")
# 载体自己的服务账号：只对 commerce 库有权限
DB_USER = os.getenv("COMMERCE_SVC_USER", "commerce_svc")
DB_PASSWORD = os.getenv("COMMERCE_SVC_PASSWORD", "commerce_local_svc")

# 接口鉴权：Agent 侧必须带这个 token，否则 401
API_TOKEN = os.getenv("COMMERCE_API_TOKEN", "local-commerce-token")
