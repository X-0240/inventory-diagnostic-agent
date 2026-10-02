"""长期记忆：偏好/约束的记住、召回、遗忘（第三档能力，落到补货场景）。

设计要点：
- 记忆是「状态 + 事件」：本表存当前状态（ACTIVE/EXPIRED/SUPERSEDED），审计表存每次变更
- 同一个 (范围, 键, 种类) 只允许一条 ACTIVE —— 库层用生成列 + 唯一索引强制，不靠应用层自觉
- 遗忘有两类：显式遗忘（人叫它忘）与过期（到点自动失效），过期由 sweep_expired 在每轮计划开头扫
- 记忆只作为「证据」进入建议与归因，不直接改数量、也不直接放行写操作（护栏仍然说了算）
"""

from datetime import datetime

from inv_agent import db, repository

SCOPES = ("GLOBAL", "SUPPLIER", "SKU")
KINDS = (
    "PREFER_ACTION",
    "AVOID_ACTION",
    "REQUIRE_HUMAN",
    "LEADTIME_NOTE",
    "SERVICE_LEVEL_NOTE",
)


class MemoryError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def remember(
    scope,
    scope_key,
    kind,
    value,
    note=None,
    source="HUMAN",
    confidence=1.0,
    expires_at=None,
    actor="human",
):
    """记住一条偏好；同键同种类的旧记忆标记为被取代（不是删除，保留可追溯）。"""
    if scope not in SCOPES:
        raise MemoryError("SCHEMA_INVALID", "范围不在枚举内: " + str(scope))
    if kind not in KINDS:
        raise MemoryError("SCHEMA_INVALID", "种类不在枚举内: " + str(kind))
    if not scope_key:
        raise MemoryError("SCHEMA_INVALID", "scope_key 不能为空")
    with db.tx() as cur:
        cur.execute(
            "SELECT id FROM preference_memory WHERE scope=%s AND scope_key=%s AND kind=%s "
            "AND status='ACTIVE'",
            (scope, scope_key, kind),
        )
        old = cur.fetchone()
        cur.execute(
            "INSERT INTO preference_memory (scope,scope_key,kind,value_json,note,confidence,"
            "source,expires_at,created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                scope,
                scope_key,
                kind,
                repository.dumps(value),
                note,
                float(confidence),
                source,
                expires_at,
                actor,
            ),
        )
        new_id = cur.lastrowid
        if old:
            cur.execute(
                "UPDATE preference_memory SET status='SUPERSEDED',superseded_by=%s WHERE id=%s",
                (new_id, old["id"]),
            )
    repository.audit(
        "preference_memory",
        new_id,
        "MEMORY_REMEMBERED",
        actor,
        "SYSTEM",
        {
            "scope": scope,
            "scope_key": scope_key,
            "kind": kind,
            "superseded": old["id"] if old else None,
            "expires_at": str(expires_at) if expires_at else None,
        },
    )
    return {"memory_id": new_id, "superseded": old["id"] if old else None}


def recall(scope, scope_key, kind=None, now=None):
    """召回仍然有效的记忆：状态 ACTIVE 且未过期。过期但还没被扫到的不返回。"""
    now = now or datetime.utcnow()
    sql = (
        "SELECT id,scope,scope_key,kind,value_json,note,confidence,source,expires_at,updated_at "
        "FROM preference_memory WHERE status='ACTIVE' AND scope=%s AND scope_key=%s "
        "AND (expires_at IS NULL OR expires_at>%s)"
    )
    params = [scope, scope_key, now]
    if kind:
        sql += " AND kind=%s"
        params.append(kind)
    sql += " ORDER BY confidence DESC, updated_at DESC"
    return db.query_all(sql, tuple(params))


def recall_for_sku(sku_code, supplier_code=None, now=None):
    """一个 SKU 相关的全部有效记忆：SKU 级 + 其供应商级 + 全局级。"""
    out = list(recall("SKU", sku_code, now=now))
    if supplier_code:
        out += list(recall("SUPPLIER", supplier_code, now=now))
    out += list(recall("GLOBAL", "-", now=now))
    return out


def describe(memories):
    """把记忆压成给模型/审批人看的一行行文本（不放 JSON 原文，避免噪声）。"""
    lines = []
    for m in memories:
        value = m["value_json"] if isinstance(m["value_json"], dict) else {}
        lines.append(
            "%s/%s %s：%s"
            % (
                m["scope"],
                m["scope_key"],
                m["kind"],
                value.get("text") or repository.dumps(value),
            )
        )
    return lines


def forget(memory_id, actor, reason):
    """显式遗忘：置为 EXPIRED 并留审计（不物理删除，保证可追溯）。"""
    with db.tx() as cur:
        cur.execute(
            "UPDATE preference_memory SET status='EXPIRED' WHERE id=%s AND status='ACTIVE'",
            (memory_id,),
        )
        changed = cur.rowcount
    repository.audit(
        "preference_memory",
        memory_id,
        "MEMORY_FORGOTTEN",
        actor,
        "SYSTEM",
        {"reason": reason},
    )
    return changed


def sweep_expired(now=None, actor="system"):
    """过期扫描：把到点的 ACTIVE 记忆置为 EXPIRED；返回处理条数。"""
    now = now or datetime.utcnow()
    with db.tx() as cur:
        cur.execute(
            "SELECT id FROM preference_memory WHERE status='ACTIVE' AND expires_at IS NOT NULL "
            "AND expires_at<=%s",
            (now,),
        )
        ids = [r["id"] for r in cur.fetchall()]
        if ids:
            cur.execute(
                "UPDATE preference_memory SET status='EXPIRED' WHERE id IN ("
                + ",".join(["%s"] * len(ids))
                + ")",
                tuple(ids),
            )
    if ids:
        repository.audit(
            "preference_memory",
            0,
            "MEMORY_SWEPT",
            actor,
            "SYSTEM",
            {"expired_ids": ids, "count": len(ids)},
        )
    return len(ids)


def stats():
    rows = db.query_all(
        "SELECT status,COUNT(*) AS n FROM preference_memory GROUP BY status"
    )
    return {r["status"]: r["n"] for r in rows}
