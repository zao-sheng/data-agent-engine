"""只读 SQL 执行器：翻译引擎产出的 SQL 在这里真实执行并返回结果集。

安全约束：
  * 只允许查询语句（SELECT/CTE），禁写禁 DDL（双重拦截：正则 + SQLite 只读模式）
  * 结果行数上限 MAX_ROWS
  * SQLite 模式注册 iso_week() 方言函数（翻译引擎 week 粒度表达式依赖它）
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
    def __init__(self, dsn: str, dialect: str = "sqlite"):
        """dsn: SQLite 文件路径（真实数仓模式：换驱动实现，SQL 不变）"""
        self.dsn = dsn
        self.dialect = dialect

    def execute(self, sql: str) -> dict:
        if FORBIDDEN.search(sql):
            return {"error": "只读执行器拒绝非查询语句"}
        t0 = time.time()
        try:
            conn = sqlite3.connect(f"file:{self.dsn}?mode=ro", uri=True)
            conn.create_function("iso_week", 1, _iso_week)
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
