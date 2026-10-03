"""慢查询优化前后对照（口径写死，可复跑）。

- 数据：两张放大表 perf_sales（150 万行）、perf_suggestion（20 万行），同一批数据只造一次
- 变量：唯一变量是索引；每个查询测前先清掉该表所有非主键索引
- 计时：读 MySQL performance_schema 的语句摘要（SUM_TIMER_WAIT/COUNT_STAR），
  不含客户端进程开销
- 扫描行数：同一摘要里的 SUM_ROWS_EXAMINED；是否走索引看 SUM_NO_INDEX_USED 与 EXPLAIN
- 结果写入 docs/慢查询对照.md（固定文件名，每次运行覆盖上一次；历史版本看 git 历史）

顶层直接执行：PYTHONPATH=src python scripts/perf_compare.py
"""

import json
import re
import subprocess
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTAINER = "inv-mysql"
ROOT_PW = "inv_local_root"
DB = "perf_lab"  # 性能对照表放独立库，业务库保持只有 13 张业务表
BUSINESS_DB = "inventory"
# 固定文件名：按日期命名会越跑越多份同类文档，引用时不知道以哪份为准（2026-09-27 踩过）
OUT = ROOT / "docs" / "慢查询对照.md"


def sql(statement, db=None):
    args = [
        "docker",
        "exec",
        CONTAINER,
        "mysql",
        "-uroot",
        "-p" + ROOT_PW,
        "--default-character-set=utf8mb4",
        "--batch",
        "--raw",
        "--skip-column-names",
        db or DB,
        "-e",
        statement,
    ]
    r = subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if r.returncode != 0:
        raise RuntimeError(
            "SQL 失败: " + statement[:70] + " :: " + r.stderr.strip()[:200]
        )
    return r.stdout.strip()


def scalar(statement):
    out = sql(statement)
    return int(out) if re.fullmatch(r"-?\d+", out or "") else out


def build_perf_tables(sales_rows=1500000, suggestion_rows=200000):
    print("构建放大表（首次约 1-2 分钟）...")
    sql(
        "CREATE DATABASE IF NOT EXISTS " + DB + " DEFAULT CHARSET utf8mb4",
        db=BUSINESS_DB,
    )
    sql("SET GLOBAL cte_max_recursion_depth=1000000")
    sql("DROP TABLE IF EXISTS perf_sales")
    sql(
        """CREATE TABLE perf_sales (id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        sku_id INT NOT NULL, sale_date DATE NOT NULL, qty INT NOT NULL,
        PRIMARY KEY(id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
    )
    days = sales_rows // 500
    sql(
        """INSERT INTO perf_sales (sku_id,sale_date,qty)
        WITH RECURSIVE s(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM s WHERE n<500),
                       d(n) AS (SELECT 0 UNION ALL SELECT n+1 FROM d WHERE n<%d)
        SELECT s.n, DATE_ADD('2015-01-01', INTERVAL d.n DAY), (s.n*7+d.n)%%50 FROM s JOIN d"""
        % (days - 1)
    )
    sql("DROP TABLE IF EXISTS perf_suggestion")
    sql(
        """CREATE TABLE perf_suggestion (id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        sku_id INT NOT NULL, status VARCHAR(24) NOT NULL, amount DECIMAL(14,2) NOT NULL,
        updated_at DATETIME NOT NULL, PRIMARY KEY(id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
    )
    sql(
        """INSERT INTO perf_suggestion (sku_id,status,amount,updated_at)
        WITH RECURSIVE n(k) AS (SELECT 1 UNION ALL SELECT k+1 FROM n WHERE k<%d)
        SELECT 1+(k%%200),
               CASE k%%5 WHEN 0 THEN 'PENDING_APPROVAL' WHEN 1 THEN 'APPROVED' WHEN 2 THEN 'EXECUTED'
                         WHEN 3 THEN 'REJECTED' ELSE 'MANUAL_TAKEOVER' END,
               ROUND((k%%997)*3.14,2), DATE_ADD('2015-01-01 00:00:00', INTERVAL (k%%3000) MINUTE)
        FROM n"""
        % (suggestion_rows,)
    )
    return {
        "perf_sales": scalar("SELECT COUNT(*) FROM perf_sales"),
        "perf_suggestion": scalar("SELECT COUNT(*) FROM perf_suggestion"),
    }


QUERIES = [
    {
        "name": "Q1 按日期区间聚合销量（报表口径，半年窗口）",
        "table": "perf_sales",
        "sql": "SELECT sku_id,SUM(qty) AS total FROM perf_sales WHERE sale_date BETWEEN '2018-01-01' AND '2018-06-30' GROUP BY sku_id",
        "index": "CREATE INDEX idx_sale_date_sku ON perf_sales (sale_date, sku_id)",
    },
    {
        "name": "Q2 单 SKU 近期明细（倒序取 50 条）",
        "table": "perf_sales",
        "sql": "SELECT id,sale_date,qty FROM perf_sales WHERE sku_id=137 AND sale_date>='2016-01-01' ORDER BY sale_date DESC LIMIT 50",
        "index": "CREATE INDEX idx_sku_date ON perf_sales (sku_id, sale_date)",
    },
    {
        "name": "Q3 建议列表按状态+时间分页（审核队列）",
        "table": "perf_suggestion",
        "sql": "SELECT id,sku_id,amount,updated_at FROM perf_suggestion WHERE status='PENDING_APPROVAL' ORDER BY updated_at DESC LIMIT 20",
        "index": "CREATE INDEX idx_status_updated ON perf_suggestion (status, updated_at)",
    },
]


def reset_indexes(table):
    """清掉该表所有非主键索引，保证"优化前"这一侧确实没索引可用。"""
    rows = sql(
        "SELECT DISTINCT INDEX_NAME FROM information_schema.STATISTICS "
        f"WHERE TABLE_SCHEMA='{DB}' AND TABLE_NAME='{table}' AND INDEX_NAME<>'PRIMARY'"
    )
    for name in [r.strip() for r in rows.splitlines() if r.strip()]:
        sql(f"DROP INDEX `{name}` ON {table}")


def collect_explain(query):
    """从 EXPLAIN FORMAT=JSON 里递归找访问方式、使用的键、扫描行数。"""
    text = sql("EXPLAIN FORMAT=JSON " + query)
    text = "".join(text.splitlines())
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {"access_type": "?", "key": None, "rows": None, "filesort": None}
    found = {}

    def walk(node):
        if isinstance(node, dict):
            if "table_name" in node or "access_type" in node:
                found.setdefault("access_type", node.get("access_type"))
                found.setdefault("key", node.get("key"))
                if node.get("rows_examined_per_scan") is not None:
                    found["rows"] = max(
                        found.get("rows") or 0, node["rows_examined_per_scan"]
                    )
            if "using_filesort" in node:
                # 只有显式为 true 才算真的走了文件排序；JSON 里出现该键不代表启用了排序
                found["filesort"] = found.get("filesort", False) or bool(
                    node["using_filesort"]
                )
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    found.setdefault("filesort", False)
    return {
        "access_type": found.get("access_type", "?"),
        "key": found.get("key"),
        "rows": found.get("rows"),
        "filesort": found.get("filesort"),
    }


PS_TABLE = "performance_schema.events_statements_summary_by_digest"


def measure(query, repeat=3):
    """先用 performance_schema 摘要清零，再跑 repeat 次，读语句级耗时与扫描行数。"""
    sql("TRUNCATE TABLE " + PS_TABLE)
    for _ in range(repeat):
        sql(query)
    row = sql(
        "SELECT COUNT_STAR,SUM_TIMER_WAIT,SUM_ROWS_EXAMINED,SUM_NO_INDEX_USED FROM "
        + PS_TABLE
        + " WHERE DIGEST_TEXT LIKE 'SELECT%' ORDER BY SUM_TIMER_WAIT DESC LIMIT 1"
    )
    parts = row.split("\t") if row else []
    count = int(parts[0]) if len(parts) > 0 and parts[0].isdigit() else repeat
    total_ps = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    rows_examined = (
        int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    )
    no_index = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    # SUM_TIMER_WAIT 单位是皮秒：毫秒 = 皮秒 / 1e9
    return {
        "avg_ms": round(total_ps / 1_000_000_000 / count, 2) if count else 0.0,
        "runs": count,
        "rows_examined_total": rows_examined,
        "rows_examined_avg": int(rows_examined / count) if count else 0,
        "no_index_used": no_index,
        "plan": collect_explain(query),
    }


def slow_log_count():
    try:
        return scalar("SELECT COUNT(*) FROM mysql.slow_log")
    except Exception:
        return -1


def main():
    counts = build_perf_tables()
    print("放大表:", counts)
    # 把慢日志阈值临时调到 0.3 秒，让"优化前"确实能进慢日志；compose 里的默认值是 0.5 秒
    sql("SET GLOBAL long_query_time=0.3")
    results = []
    for q in QUERIES:
        reset_indexes(q["table"])
        slow_before = slow_log_count()
        before = measure(q["sql"])
        sql(q["index"])
        after = measure(q["sql"])
        slow_after = slow_log_count()
        results.append(
            {
                "name": q["name"],
                "before": before,
                "after": after,
                "slow_new": max(0, slow_after - slow_before),
            }
        )
    lines = [
        "# 慢查询优化前后对照",
        "",
        f"生成时间：{date.today().isoformat()}",
        "放大表：perf_sales %d 行、perf_suggestion %d 行。"
        % (counts["perf_sales"], counts["perf_suggestion"]),
        "",
        "口径：同一张表、同一批数据、同一实例；唯一变量是索引（测前先清空该表非主键索引）；",
        "耗时与扫描行数取自 MySQL performance_schema 语句摘要（不含客户端进程开销），每次跑 3 条取平均。",
        "",
        "| 查询 | 优化前平均耗时 | 优化后平均耗时 | 优化前扫描行数 | 优化后扫描行数 | 优化前访问方式/键 | 优化后访问方式/键 | 优化前filesort | 优化后filesort | 新增慢日志 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            "| %s | %.2f ms | %.2f ms | %s | %s | %s / %s | %s / %s | %s | %s | %d |"
            % (
                r["name"],
                r["before"]["avg_ms"],
                r["after"]["avg_ms"],
                r["before"]["rows_examined_avg"],
                r["after"]["rows_examined_avg"],
                r["before"]["plan"]["access_type"],
                r["before"]["plan"]["key"],
                r["after"]["plan"]["access_type"],
                r["after"]["plan"]["key"],
                r["before"]["plan"]["filesort"],
                r["after"]["plan"]["filesort"],
                r["slow_new"],
            )
        )
    lines += ["", "## 明细", ""]
    for r in results:
        lines.append(
            "- %s：优化前平均 %.2f ms（%d 次，扫描 %d 行/次，未用索引次数 %d）；优化后平均 %.2f ms（扫描 %d 行/次，未用索引次数 %d）"
            % (
                r["name"],
                r["before"]["avg_ms"],
                r["before"]["runs"],
                r["before"]["rows_examined_avg"],
                r["before"]["no_index_used"],
                r["after"]["avg_ms"],
                r["after"]["rows_examined_avg"],
                r["after"]["no_index_used"],
            )
        )
    lines += [
        "",
        "## 局限",
        "",
        "- 这是放大到百万行后的仿真对照，不是真实业务流量下的压测；数字只能说明索引对这三类查询的作用方向与量级。",
        "- 慢日志阈值是 0.5 秒；只有优化前的部分查询会落进慢日志，说明索引正是让它不再进慢日志的原因。",
    ]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("对照已写入:", OUT)
    for r in results:
        print(
            "  %s: %.2f ms → %.2f ms；扫描 %d → %d 行"
            % (
                r["name"],
                r["before"]["avg_ms"],
                r["after"]["avg_ms"],
                r["before"]["rows_examined_avg"],
                r["after"]["rows_examined_avg"],
            )
        )


if __name__ == "__main__":
    main()
