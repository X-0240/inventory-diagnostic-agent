"""MySQL 连接与事务辅助：所有写操作都经这里，便于统一加审计与条件更新。"""
from contextlib import contextmanager

import pymysql
from pymysql.cursors import DictCursor

from inv_agent import config

def connect(autocommit=False):
    return pymysql.connect(host=config.DB_HOST,port=config.DB_PORT,user=config.DB_USER,
        password=config.DB_PASSWORD,database=config.DB_NAME,charset="utf8mb4",
        cursorclass=DictCursor,autocommit=autocommit)

@contextmanager
def tx():
    """一个事务；异常回滚。所有多表写入必须走这里。"""
    conn=connect()
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def query_all(sql,params=None):
    conn=connect(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(sql,params or ())
            return cur.fetchall()
    finally:
        conn.close()

def query_one(sql,params=None):
    rows=query_all(sql,params)
    return rows[0] if rows else None

def execute(sql,params=None):
    with tx() as cur:
        return cur.execute(sql,params or ())

def ping():
    conn=connect(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 AS ok")
            return cur.fetchone()
    finally:
        conn.close()
