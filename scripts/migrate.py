"""建 Agent 库与应用账号，并按文件名顺序执行 migrations 下的 SQL（幂等，可重复跑）。

直连本机 MySQL（不再依赖容器）：python scripts/migrate.py
"""

import sys
from pathlib import Path

import pymysql
from pymysql.converters import escape_string

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"


def load_env():
    # 只取键值给连接用，不打印任何取值，避免密码落进日志
    env = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def connect(database=None):
    # 建库建号与执行迁移都要 DDL 权限，所以走 root
    return pymysql.connect(
        host=HOST,
        port=PORT,
        user="root",
        password=ROOT_PW,
        database=database,
        charset="utf8mb4",
        autocommit=True,
        client_flag=pymysql.constants.CLIENT.MULTI_STATEMENTS,
    )


def run_sql_file(conn, path):
    # 整份文件交给服务端执行，自己不做语句切分（避免把字符串里的分号切坏）
    with conn.cursor() as cur:
        cur.execute(path.read_text(encoding="utf-8"))
        while cur.nextset():
            pass


env = load_env()
HOST = "127.0.0.1"
PORT = int(env.get("MYSQL_PORT", "3306"))
DB = env.get("MYSQL_DATABASE", "inventory")
APP_USER = env.get("MYSQL_APP_USER", "inv_app")
APP_PW = env.get("MYSQL_APP_PASSWORD", "")
ROOT_PW = env.get("MYSQL_ROOT_PASSWORD", "")

files = sorted(MIGRATIONS.glob("*.sql"))
if not files:
    print("migrations 下没有 SQL 文件")
    sys.exit(1)

# 原生 MySQL 没有容器的初始化钩子，库与账号由这里补，账号密码以 .env 为准
conn = connect()
with conn.cursor() as cur:
    cur.execute(
        f"CREATE DATABASE IF NOT EXISTS `{DB}` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
    )
    user = f"'{escape_string(APP_USER)}'@'%'"
    cur.execute(
        f"CREATE USER IF NOT EXISTS {user} IDENTIFIED BY '{escape_string(APP_PW)}'"
    )
    cur.execute(f"ALTER USER {user} IDENTIFIED BY '{escape_string(APP_PW)}'")
    cur.execute(f"GRANT ALL PRIVILEGES ON `{DB}`.* TO {user}")
conn.close()

conn = connect(DB)
for f in files:
    try:
        run_sql_file(conn, f)
        print(f"[ok] {f.name}")
    except Exception as e:
        print(f"[FAIL] {f.name}: {e}")
        conn.close()
        sys.exit(1)
conn.close()
print(
    "迁移完成，共",
    len(files),
    "个文件；应用账号",
    APP_USER,
    "只被授予",
    DB,
    "库权限",
)
