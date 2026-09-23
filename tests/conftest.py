"""测试夹具：把 src 加入路径，并提供"数据库可用"的跳过判定。"""
import sys
from pathlib import Path

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))

@pytest.fixture(scope="session")
def db_ready():
    from inv_agent import db
    try:
        db.ping()
    except Exception as e:
        pytest.skip("MySQL 不可用，跳过数据库相关用例: "+str(e))
    return True
