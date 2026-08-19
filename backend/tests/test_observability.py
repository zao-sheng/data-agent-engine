"""P2 可观测性测试：启动自检 + 健康检查状态 + 运行日志轮转。

运行：backend/.venv/bin/python -m unittest tests.test_observability
"""
from __future__ import annotations

import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from core import Executor, Ontology  # noqa: E402
from core.startup_check import run_startup_checks, startup_status  # noqa: E402


class StartupCheckTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.onto = Ontology(BACKEND / "ontology")
        cls.executor = Executor(str(BACKEND / "seed" / "sample.db"))

    def test_all_required_checks_pass(self):
        checks = run_startup_checks(self.onto, self.executor)
        names = [c.name for c in checks]
        self.assertEqual(names, ["ontology", "db", "log_dir"])
        for c in checks:
            self.assertTrue(c.ok, f"{c.name} 应通过: {c.detail}")

    def test_status_snapshot_shape(self):
        status = startup_status(self.onto, self.executor)
        self.assertTrue(status["ok"])
        self.assertEqual(status["dialect"], "sqlite")
        self.assertGreater(status["ontology"]["objects"], 0)
        self.assertGreater(status["ontology"]["functions"], 0)
        self.assertEqual(len(status["checks"]), 3)
        # checks 可序列化（健康检查返回给 LLM）
        json.dumps(status)

    def test_missing_db_fails_check(self):
        ex = Executor("/nonexistent/sample.db")
        checks = run_startup_checks(self.onto, ex)
        db = [c for c in checks if c.name == "db"][0]
        self.assertFalse(db.ok)

    def test_remote_dialect_without_dsn_is_non_required_failure(self):
        from core import config as cfg
        saved = cfg.CONFIG.remote_dsn
        cfg.CONFIG.remote_dsn = {}
        try:
            ex = Executor("mysql://x", dialect="mysql")
            checks = run_startup_checks(self.onto, ex)
            db = [c for c in checks if c.name == "db"][0]
            self.assertFalse(db.ok)
            self.assertFalse(db.required)  # 未配置远程 = 该方言不可用，不算引擎故障
        finally:
            cfg.CONFIG.remote_dsn = saved


class RuntimeLogTest(unittest.TestCase):
    def setUp(self):
        import importlib
        import core.runtime_log as rl
        rl._logger = None
        logging.getLogger("data-agent.runtime").handlers.clear()
        self.rl = rl
        self.log_dir = Path(tempfile.mkdtemp(prefix="da-runtime-"))

    def test_structured_log_emission(self):
        self.rl.setup_runtime_logger(self.log_dir, level="INFO", max_bytes=1024, backup_count=2)
        self.rl.log_info("startup_check_ok", name="ontology", detail="objects=8")
        self.rl.log_warn("warn_event", reason="x")
        lines = (self.log_dir / "runtime.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 2)
        rec = json.loads(lines[0])
        self.assertEqual(rec["event"], "startup_check_ok")
        self.assertEqual(rec["name"], "ontology")
        self.assertIn("ts", rec)

    def test_rotation_limits_growth(self):
        self.rl.setup_runtime_logger(self.log_dir, level="INFO", max_bytes=512, backup_count=2)
        for i in range(200):
            self.rl.log_info("event", idx=i)
        files = list(self.log_dir.glob("runtime.jsonl*"))
        self.assertLessEqual(len(files), 3)


if __name__ == "__main__":
    unittest.main()
