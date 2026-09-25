"""载体库连接：与 Agent 的 db.py 同构，但连的是独立库与独立账号。"""
from contextlib import contextmanager
import threading

import pymysql
from pymysql.cursors import DictCursor

from commerce_core import config

_local=threading.local()

def connect(autocommit=False):
    return pymysql.connect(host=config.DB_HOST,port=config.DB_PORT,user=config.DB_USER,
        password=config.DB_PASSWORD,database=config.DB_NAME,charset="utf8mb4",
        cursorclass=DictCursor,autocommit=autocommit)

def _thread_conn():
    conn=getattr(_local,"conn",None)
    if conn is None:
        conn=connect(autocommit=True)
        _local.conn=conn
        return conn
    try:
        conn.ping(reconnect=True)
    except Exception:
        conn=connect(autocommit=True)
        _local.conn=conn
    return conn

def close_thread_conn():
    conn=getattr(_local,"conn",None)
    if conn is not None:
        conn.close()
        _local.conn=None

@contextmanager
def tx():
    """一个事务；异常回滚。嵌套调用加入外层事务。"""
    conn=_thread_conn()
    depth=getattr(_local,"depth",0)
    _local.depth=depth+1
    try:
        with conn.cursor() as cur:
            yield cur
        if depth==0:
            conn.commit()
    except Exception:
        if depth==0:
            conn.rollback()
        raise
    finally:
        _local.depth=depth

def query_all(sql,params=None):
    conn=_thread_conn()
    with conn.cursor() as cur:
        cur.execute(sql,params or ())
        return cur.fetchall()

def query_one(sql,params=None):
    rows=query_all(sql,params)
    return rows[0] if rows else None

def execute(sql,params=None):
    with tx() as cur:
        return cur.execute(sql,params or ())
