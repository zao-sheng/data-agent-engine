"""只读 SQL 执行器：翻译引擎产出的 SQL 在这里真实执行并返回结果集。

安全约束：
  * 只允许查询语句（SELECT/CTE），禁写禁 DDL（双重拦截：正则 + SQLite 只读模式）
  * 结果行数上限 MAX_ROWS
  * SQLite 模式注册 iso_week() 方言函数（翻译引擎 week 粒度表达式依赖它）

扩展真实数仓（Doris/Hive/ClickHouse/StarRocks）：
  1. 保持翻译引擎输出 SQL 不变（方言由 Translator 的 dialect 参数控制）；
  2. 新增子类覆盖 `_execute_remote`（或直接改写 `execute`），用对应驱动执行；
  3. 各引擎特有的标量函数（如 iso_week 等价物）在 `_register_dialect_functions`
     里按引擎注册，或改为翻译引擎输出该引擎原生表达式。
"""
from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime

MAX_ROWS = 1000
FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|vacuum|reindex)\b", re.I)


def _iso_week(dstr: str) -> int:
    """'YYYYMMDD' → ISO 周数（SQLite 无法解析紧凑格式，由本函数兜底）"""
    return int(datetime.strptime(dstr, "%Y%m%d").strftime("%V"))


class Executor:
    """只读执行器。默认 SQLite；真实数仓通过覆盖 `_execute_remote` 接入。"""

    def __init__(self, dsn: str, dialect: str = "sqlite"):
        self.dsn = dsn
        self.dialect = dialect

    def execute(self, sql: str) -> dict:
        if FORBIDDEN.search(sql):
            return {"error": "只读执行器拒绝非查询语句"}
        if self.dialect == "sqlite" or self.dsn.endswith(".db"):
            return self._execute_sqlite(sql)
        return self._execute_remote(sql)

    # ── SQLite 实现 ──────────────────────────────────────────
    def _execute_sqlite(self, sql: str) -> dict:
        t0 = time.time()
        try:
            conn = sqlite3.connect(f"file:{self.dsn}?mode=ro", uri=True)
            self._register_dialect_functions(conn)
            conn.row_factory = sqlite3.Row
            cur = conn.execute(sql)
            rows = [dict(r) for r in cur.fetchmany(MAX_ROWS + 1)]
            conn.close()
            truncated = len(rows) > MAX_ROWS
            rows = rows[:MAX_ROWS]
        except sqlite3.Error as e:
            return {"error": f"SQL 执行失败: {e}"}
        return {
            "columns": list(rows[0].keys()) if rows else [],
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
            "elapsed_ms": int((time.time() - t0) * 1000),
        }

    @staticmethod
    def _register_dialect_functions(conn: sqlite3.Connection) -> None:
        """SQLite 方言函数：iso_week（翻译引擎 week 粒度表达式依赖）"""
        conn.create_function("iso_week", 1, _iso_week)

    # ── 真实数仓（扩展点）────────────────────────────────────
    def _execute_remote(self, sql: str) -> dict:
        """真实数仓执行：用对应驱动（pymysql / doris / clickhouse-driver…）实现。
        需自行加上：行数上限、超时、结果集封装、PII 脱敏钩子。"""
        raise NotImplementedError(
            f"真实数仓模式未实现（dialect={self.dialect}）。"
            "请覆盖 _execute_remote：连接你的数仓，执行 SQL，返回 "
            "{columns, rows, row_count, truncated, elapsed_ms}。")
