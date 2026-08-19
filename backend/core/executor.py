"""只读 SQL 执行器：翻译引擎产出的 SQL 在这里真实执行并返回结果集。

安全约束：
  * 只允许查询语句（SELECT/CTE），禁写禁 DDL（双重拦截：正则 + SQLite 只读模式）
  * 结果行数上限 MAX_ROWS
  * SQLite 模式注册 iso_week() 方言函数（翻译引擎 week 粒度表达式依赖它）
  * 远程执行（mysql/doris/hive/sparksql）：连接串来自 backend/.env 的
    DATA_AGENT_DSN_<方言>，只读账号由企业侧保障；本执行器只做 SELECT 正则
    拦截 + 行数上限 + 连接超时。

方言与介质：
  * sqlite —— 本地样例库（已实现已测试）
  * mysql / doris —— pymysql 驱动（Doris 兼容 MySQL 协议）
  * hive / sparksql —— pyhive 驱动（Thrift）
  远程方言的 SQL 由 Translator 按 dialect 生成；本层只负责连接与取数。
"""
from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime
from urllib.parse import urlparse

from .config import CONFIG

MAX_ROWS = 1000
CONNECT_TIMEOUT_S = int(getattr(CONFIG, "remote_timeout_s", 15))
FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|vacuum|reindex)\b", re.I)

# 方言 → 连接 scheme（DSN 前缀）。hive/sparksql 走 Thrift，需独立驱动。
MYSQL_LIKE = {"mysql", "doris"}
HIVE_LIKE = {"hive", "sparksql"}


def _iso_week(dstr: str) -> int:
    """'YYYYMMDD' → ISO 周数（SQLite 无法解析紧凑格式，由本函数兜底）"""
    return int(datetime.strptime(dstr, "%Y%m%d").strftime("%V"))


class Executor:
    """只读执行器。默认 SQLite；真实数仓通过 `.env` 的 DATA_AGENT_DSN_* 接入。"""

    def __init__(self, dsn: str, dialect: str = "sqlite"):
        """dsn: 数据介质（SQLite 文件路径 / 真实数仓连接串）
        dialect: SQL 方言（sqlite 本地 / mysql / doris / hive / sparksql 远程）"""
        self.dsn = dsn
        self.dialect = dialect

    def execute(self, sql: str) -> dict:
        if FORBIDDEN.search(sql):
            return {"error": "只读执行器拒绝非查询语句"}
        if self.dialect == "sqlite":
            return self._execute_sqlite(sql)
        # 远程方言：dsn 是 SQLite 文件路径说明介质不匹配（配置错误）
        if self.dsn.endswith(".db"):
            return {"error": f"方言 {self.dialect} 与 SQLite 介质不匹配："
                             f"请在 backend/.env 配置 DATA_AGENT_DSN_{self.dialect.upper()}"
                             "（格式 scheme://user:pass@host:port/db）"}
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

    # ── 真实数仓（P1-5）──────────────────────────────────────
    def _execute_remote(self, sql: str) -> dict:
        """按方言从配置读取连接串并执行。驱动缺失时给出安装指引。"""
        dsn = CONFIG.dsn_for(self.dialect)
        if not dsn:
            return {"error": f"未配置 {self.dialect} 连接串：请在 backend/.env 设置 "
                             f"DATA_AGENT_DSN_{self.dialect.upper()}=scheme://user:pass@host:port/db"}
        t0 = time.time()
        try:
            rows, truncated = self._run_remote(dsn, sql)
        except ImportError as e:
            return {"error": f"缺少 {self.dialect} 驱动: {e}。"
                             f"安装: uv pip install --python backend/.venv/bin/python {_driver_pkg(self.dialect)}"}
        except Exception as e:  # noqa: BLE001 —— 远程连接/执行错误统一封装
            return {"error": f"{self.dialect} 执行失败: {e}"}
        return {
            "columns": list(rows[0].keys()) if rows else [],
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
            "elapsed_ms": int((time.time() - t0) * 1000),
        }

    def _run_remote(self, dsn: str, sql: str) -> tuple[list[dict], bool]:
        """执行远程 SQL，返回 (rows, truncated)。驱动缺失抛 ImportError。"""
        scheme = dsn.split("://", 1)[0].lower()
        if scheme in MYSQL_LIKE:
            return self._run_mysql_like(dsn, sql)
        if scheme in HIVE_LIKE:
            return self._run_hive_like(dsn, sql)
        raise ValueError(f"不支持的连接 scheme: {scheme}（支持 {sorted(MYSQL_LIKE | HIVE_LIKE)}）")

    # ── MySQL / Doris（pymysql，Doris 兼容 MySQL 协议）────────
    def _run_mysql_like(self, dsn: str, sql: str) -> tuple[list[dict], bool]:
        try:
            import pymysql
        except ImportError:
            raise ImportError("pymysql")
        u = urlparse(dsn)
        conn = pymysql.connect(
            host=u.hostname or "127.0.0.1",
            port=u.port or 3306,
            user=u.username or "",
            password=u.password or "",
            database=(u.path or "/").lstrip("/") or None,
            connect_timeout=CONNECT_TIMEOUT_S,
            read_timeout=CONNECT_TIMEOUT_S,
            cursorclass=pymysql.cursors.DictCursor,
        )
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchmany(MAX_ROWS + 1)
        finally:
            conn.close()
        truncated = len(rows) > MAX_ROWS
        return rows[:MAX_ROWS], truncated

    # ── Hive / SparkSQL（pyhive Thrift）──────────────────────
    def _run_hive_like(self, dsn: str, sql: str) -> tuple[list[dict], bool]:
        try:
            from pyhive import hive as hive_client
        except ImportError:
            raise ImportError("pyhive thrift")
        u = urlparse(dsn)
        conn = hive_client.connect(
            host=u.hostname or "127.0.0.1",
            port=u.port or 10000,
            username=u.username or "default",
            password=u.password or "",
            database=(u.path or "/").lstrip("/") or "default",
            timeout=CONNECT_TIMEOUT_S,
        )
        try:
            cur = conn.cursor()
            cur.execute(sql)
            cols = [d[0] for d in cur.description or []]
            raw = cur.fetchmany(MAX_ROWS + 1)
        finally:
            conn.close()
        truncated = len(raw) > MAX_ROWS
        rows = [dict(zip(cols, r)) for r in raw[:MAX_ROWS]]
        return rows, truncated


def _driver_pkg(dialect: str) -> str:
    """方言 → 缺驱动时的安装包提示。"""
    if dialect in MYSQL_LIKE:
        return "pymysql"
    if dialect in HIVE_LIKE:
        return "pyhive thrift sasl"
    return "pymysql"
