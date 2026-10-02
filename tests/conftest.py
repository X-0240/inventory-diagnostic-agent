"""测试夹具：把 src 加入路径，并提供"数据库可用"的跳过判定。"""

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture(scope="session")
def db_ready():
    from inv_agent import db

    try:
        db.ping()
    except Exception as e:
        pytest.skip("MySQL 不可用，跳过数据库相关用例: " + str(e))
    return True


@pytest.fixture(scope="session")
def commerce_url():
    """载体服务地址：已在跑就复用，否则起一个子进程，用完关掉。"""
    from inv_agent import config

    url = config.COMMERCE_BASE_URL
    proc = None

    def alive(target):
        try:
            return (
                httpx.get(target + "/health", timeout=1.5).json().get("status")
                == "ok"
            )
        except Exception:
            return False

    if not alive(url):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "src")
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "commerce_core.api:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8001",
                "--log-level",
                "warning",
            ],
            cwd=str(ROOT),
            env=env,
        )
        for _ in range(40):
            if alive(url):
                break
            time.sleep(0.5)
    if not alive(url):
        pytest.skip("载体服务 commerce-core 不可用")
    yield url
    if proc:
        proc.terminate()


@pytest.fixture()
def temp_suggestion(db_ready):
    """造一条金额超过审批人上限的待审批建议，供接口层与执行器测试使用，用完清理干净。"""
    from inv_agent import db, repository

    with db.tx() as cur:
        cur.execute(
            "INSERT INTO supplier (supplier_code,name,lead_time_days,lead_time_sigma_days,"
            "payment_terms_days,credit_limit) VALUES ('TESTAPI','接口测试供应商',7,1.0,30,1000000) "
            "ON DUPLICATE KEY UPDATE name=VALUES(name)"
        )
        cur.execute("SELECT id FROM supplier WHERE supplier_code='TESTAPI'")
        supplier_id = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO sku (sku_code,name,category,supplier_id,unit_cost,price,lead_time_days,"
            "moq,pack_size,service_level) VALUES ('TEST-API-001','接口测试商品','TEST',%s,100.00,199.00,7,1,1,0.95) "
            "ON DUPLICATE KEY UPDATE unit_cost=VALUES(unit_cost)",
            (supplier_id,),
        )
        cur.execute("SELECT id FROM sku WHERE sku_code='TEST-API-001'")
        sku_id = cur.fetchone()["id"]
    # 金额 9000 > APPROVER 上限 5000：用来验证"超额必须走主管"
    suggestion_id = repository.insert_suggestion(
        sku_id=sku_id,
        period="2099-W10",
        qty=90,
        amount=9000.00,
        basis=[{"source": "test"}],
        rule_trace=[{"rule": "test"}],
        content_hash="a" * 64,
        status="PENDING_APPROVAL",
    )
    yield {
        "sku_id": sku_id,
        "supplier_id": supplier_id,
        "suggestion_id": suggestion_id,
    }
    with db.tx() as cur:
        cur.execute("DELETE FROM inbound_order WHERE sku_id=%s", (sku_id,))
        cur.execute("DELETE FROM purchase_order WHERE sku_id=%s", (sku_id,))
        cur.execute(
            "DELETE FROM approval_record WHERE suggestion_id=%s",
            (suggestion_id,),
        )
        cur.execute(
            "DELETE FROM replenishment_suggestion WHERE sku_id=%s", (sku_id,)
        )
        cur.execute("DELETE FROM exception_case WHERE sku_id=%s", (sku_id,))
        cur.execute("DELETE FROM policy_snapshot WHERE sku_id=%s", (sku_id,))
        cur.execute("DELETE FROM inventory_snapshot WHERE sku_id=%s", (sku_id,))
        cur.execute("DELETE FROM sales_daily WHERE sku_id=%s", (sku_id,))
        cur.execute("DELETE FROM sku WHERE id=%s", (sku_id,))
        cur.execute("DELETE FROM supplier WHERE id=%s", (supplier_id,))
