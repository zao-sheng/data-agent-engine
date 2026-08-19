"""P1-5 远程执行器测试：连接串解析、未配置/未装驱动时的报错路径。

真实连接不做（需要真实数仓）；这里锁定「配置缺失」「驱动缺失」等
fail-fast 路径，以及 SQL 只读拦截在远程方言下同样生效。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from core.executor import Executor  # noqa: E402


class RemoteExecutorTest(unittest.TestCase):
    def test_missing_dsn_reports_config_hint(self):
        """未配置连接串 → 明确提示 .env 配置项。"""
        ex = Executor("", dialect="mysql")
        # 手动清空 remote_dsn（测试环境可能没有 .env）
        from core import config as cfg
        saved = cfg.CONFIG.remote_dsn
        cfg.CONFIG.remote_dsn = {}
        try:
            r = ex.execute("SELECT 1")
            self.assertIn("error", r)
            self.assertIn("DATA_AGENT_DSN_MYSQL", r["error"])
            self.assertIn("scheme://user:pass@host:port/db", r["error"])
        finally:
            cfg.CONFIG.remote_dsn = saved

    def test_sqlite_medium_rejects_remote_dialect(self):
        """dsn 是 .db 文件但方言是远程 → 介质不匹配错误。"""
        ex = Executor("/tmp/x.db", dialect="doris")
        r = ex.execute("SELECT 1")
        self.assertIn("error", r)
        self.assertIn("SQLite 介质不匹配", r["error"])

    def test_forbidden_sql_rejected_before_dialect(self):
        """远程方言下禁写语句同样被拦截（不触达驱动）。"""
        ex = Executor("", dialect="mysql")
        r = ex.execute("DELETE FROM dwd_ord_pay_di")
        self.assertIn("error", r)
        self.assertIn("只读执行器拒绝", r["error"])

    def test_missing_driver_reports_install_hint(self):
        """配置了连接串但驱动未装 → 给出安装指引。"""
        from core import config as cfg
        saved = cfg.CONFIG.remote_dsn
        cfg.CONFIG.remote_dsn = {"mysql": "mysql://u:p@127.0.0.1:3306/db"}
        try:
            ex = Executor("mysql://u:p@127.0.0.1:3306/db", dialect="mysql")
            r = ex.execute("SELECT 1")
            self.assertIn("error", r)
            self.assertIn("pymysql", r["error"])
            self.assertIn("uv pip install", r["error"])
        finally:
            cfg.CONFIG.remote_dsn = saved

    def test_unsupported_scheme(self):
        from core import config as cfg
        saved = cfg.CONFIG.remote_dsn
        cfg.CONFIG.remote_dsn = {"mysql": "oracle://u:p@h:1521/db"}
        try:
            ex = Executor("oracle://u:p@h:1521/db", dialect="mysql")
            r = ex.execute("SELECT 1")
            self.assertIn("error", r)
            self.assertIn("不支持的连接 scheme", r["error"])
        finally:
            cfg.CONFIG.remote_dsn = saved


if __name__ == "__main__":
    unittest.main()
