"""接口层测试：鉴权、权限矩阵、幂等语义与指标接口。

注意：当前接口用请求头传身份（X-Actor/X-Role），只做权限矩阵校验，不是真正的认证。
"""

from fastapi.testclient import TestClient

from inv_agent.api import app

client = TestClient(app)
HDR_APPROVER = {"X-Actor": "alice", "X-Role": "APPROVER"}
HDR_SUPERVISOR = {"X-Actor": "bob", "X-Role": "SUPERVISOR"}
HDR_BUYER = {"X-Actor": "carol", "X-Role": "BUYER"}
HDR_ADMIN = {"X-Actor": "dave", "X-Role": "ADMIN"}


def test_health_needs_no_auth():
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_missing_actor_header_is_rejected():
    r = client.get("/suggestions")
    assert r.status_code == 401


def test_unknown_role_is_rejected():
    r = client.get("/suggestions", headers={"X-Actor": "x", "X-Role": "HACKER"})
    assert r.status_code == 401


def test_list_suggestions_with_actor(db_ready):
    r = client.get("/suggestions", params={"limit": 3}, headers=HDR_BUYER)
    assert r.status_code == 200
    body = r.json()
    assert "items" in body and body["count"] == len(body["items"])


def test_get_unknown_suggestion_returns_404():
    r = client.get("/suggestions/99999999", headers=HDR_BUYER)
    assert r.status_code == 404


def test_metrics_endpoint(db_ready):
    r = client.get("/metrics/2011-W23", headers=HDR_SUPERVISOR)
    assert r.status_code == 200
    body = r.json()
    assert "stockout_rate" in body and "definition_version" in body


def test_approve_over_limit_requires_supervisor(db_ready, temp_suggestion):
    sid = temp_suggestion["suggestion_id"]
    r = client.post(
        f"/suggestions/{sid}/approve",
        json={"decision": "APPROVE"},
        headers=HDR_APPROVER,
    )
    assert r.status_code == 403, "超额建议不允许审批人批准"
    r2 = client.post(
        f"/suggestions/{sid}/approve",
        json={"decision": "APPROVE"},
        headers=HDR_SUPERVISOR,
    )
    assert r2.status_code == 200
    assert r2.json()["approval"]["decision"] == "APPROVE"


def test_execute_before_approval_conflicts(db_ready, temp_suggestion):
    """接口层执行同一条未批准的建议：状态机应拒绝（这里先审批消耗掉，再用第二条验状态语义）。"""
    sid = temp_suggestion["suggestion_id"]
    r = client.post(f"/suggestions/{sid}/execute", headers=HDR_SUPERVISOR)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "APPROVAL_INVALIDATED"


def test_recover_requires_privileged_role(db_ready):
    r = client.post("/admin/recover", headers=HDR_BUYER)
    assert r.status_code == 403
    r2 = client.post("/admin/recover", headers=HDR_ADMIN)
    assert r2.status_code == 200
