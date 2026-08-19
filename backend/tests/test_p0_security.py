"""P0 安全加固测试：查询令牌（execute_sql 防绕过）+ 审计日志。

运行：uv run --project backend python -m pytest backend/tests/test_p0_security.py
或：backend/.venv/bin/python -m unittest backend.tests.test_p0_security
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from core.query_token import QueryTokenStore  # noqa: E402


class QueryTokenTest(unittest.TestCase):
    def setUp(self):
        self.store = QueryTokenStore(ttl_seconds=60, max_tokens=10)

    def test_issue_and_verify_ok(self):
        tok = self.store.issue("SELECT 1")
        ok, reason = self.store.verify(tok, "SELECT 1")
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_verify_rejects_unknown_token(self):
        ok, reason = self.store.verify("deadbeef" * 4, "SELECT 1")
        self.assertFalse(ok)
        self.assertIn("无效或已过期", reason)

    def test_verify_rejects_sql_mismatch(self):
        """防绕过：令牌绑定 SQL 原文，换 SQL 必须拒绝。"""
        tok = self.store.issue("SELECT * FROM dwd_ord_pay_di")
        ok, _ = self.store.verify(tok, "SELECT * FROM dwd_ord_pay_di")
        self.assertTrue(ok)
        ok, reason = self.store.verify(tok, "SELECT * FROM dwd_ord_pay_di LIMIT 1")
        self.assertFalse(ok)
        self.assertIn("不匹配", reason)

    def test_verify_rejects_expired(self):
        store = QueryTokenStore(ttl_seconds=-1)  # 立即过期
        tok = store.issue("SELECT 1")
        ok, reason = store.verify(tok, "SELECT 1")
        self.assertFalse(ok)
        self.assertIn("已过期", reason)

    def test_max_tokens_evicts_oldest(self):
        store = QueryTokenStore(ttl_seconds=60, max_tokens=3)
        toks = [store.issue(f"SELECT {i}") for i in range(4)]
        # 最旧的应被淘汰，其余有效
        ok_old, _ = store.verify(toks[0], "SELECT 0")
        self.assertFalse(ok_old)
        ok_new, _ = store.verify(toks[3], "SELECT 3")
        self.assertTrue(ok_new)

    def test_gc_removes_expired(self):
        store = QueryTokenStore(ttl_seconds=-1)
        store.issue("SELECT 1")           # 加入即过期
        store.issue("SELECT 2")           # 触发 GC，清掉 SELECT 1 的令牌
        self.assertEqual(len(store), 1)   # 只剩 SELECT 2 的令牌
        ok, _ = store.verify(store.issue("SELECT 3"), "SELECT 3")
        self.assertFalse(ok)              # 新令牌自身已过期（ttl=-1）

    def test_fingerprint_deterministic(self):
        self.assertEqual(QueryTokenStore.fingerprint("SELECT 1"),
                         QueryTokenStore.fingerprint("SELECT 1"))
        self.assertNotEqual(QueryTokenStore.fingerprint("SELECT 1"),
                            QueryTokenStore.fingerprint("SELECT 2"))


class AuditLogTest(unittest.TestCase):
    def setUp(self):
        self.log_dir = Path(tempfile.mkdtemp(prefix="da-audit-"))
        # 重新初始化审计 logger（模块级单例——测试环境用独立目录需重置并清空 handlers）
        import logging
        import importlib
        import core.audit as audit_mod
        audit_mod._audit = None
        logging.getLogger("data-agent.audit").handlers.clear()
        self.audit_mod = audit_mod

    def test_write_and_parse(self):
        audit = self.audit_mod.setup_audit_logger(self.log_dir, max_bytes=1024, backup_count=2)
        self.audit_mod.audit("semantic_translate", "call", "ok",
                             elapsed_ms=3, args={"mql": {"metrics": ["gmv"]}},
                             result={"row_count": 1})
        line = (self.log_dir / "audit.jsonl").read_text().strip()
        rec = json.loads(line)
        self.assertEqual(rec["tool"], "semantic_translate")
        self.assertEqual(rec["outcome"], "ok")
        self.assertEqual(rec["elapsed_ms"], 3)
        self.assertEqual(rec["args"]["mql"]["metrics"], ["gmv"])

    def test_rotation_limits_growth(self):
        audit = self.audit_mod.setup_audit_logger(self.log_dir, max_bytes=512, backup_count=2)
        for i in range(200):
            self.audit_mod.audit("execute_sql", "call", "ok", elapsed_ms=i,
                                 args={"sql_len": i}, result={"row_count": i})
        files = sorted(self.log_dir.glob("audit.jsonl*"))
        # 最多 backup_count 个滚动文件 + 当前文件
        self.assertLessEqual(len(files), 3)
        total = sum(f.stat().st_size for f in files)
        self.assertLess(total, 512 * 3 + 4096)  # 不无限增长


if __name__ == "__main__":
    unittest.main()
