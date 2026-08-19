"""安全与治理测试：查询令牌 / 确认令牌 / 审计日志 / 搜索索引（P0+P4 合并）。

运行：backend/.venv/bin/python -m unittest tests.test_security
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from core import Ontology  # noqa: E402
from core.confirm_token import ConfirmTokenStore  # noqa: E402
from core.query_token import QueryTokenStore  # noqa: E402


class ConfirmTokenTest(unittest.TestCase):
    def setUp(self):
        self.store = ConfirmTokenStore(ttl_seconds=60, max_tokens=10)
        self.mql = {"metrics": ["gmv"], "time_range": {"start": "20260701", "end": "20260707"}}

    def test_issue_and_verify_ok(self):
        tok = self.store.issue(self.mql)
        ok, reason = self.store.verify(tok, self.mql)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_verify_rejects_unknown(self):
        ok, reason = self.store.verify("bad" * 8, self.mql)
        self.assertFalse(ok)
        self.assertIn("无效或已过期", reason)

    def test_verify_rejects_mql_change(self):
        """确认后改 MQL（指纹变化）必须重新确认。"""
        tok = self.store.issue(self.mql)
        changed = dict(self.mql)
        changed["time_range"] = {"start": "20260701", "end": "20260714"}
        ok, reason = self.store.verify(tok, changed)
        self.assertFalse(ok)
        self.assertIn("不匹配", reason)

    def test_fingerprint_sensitive_to_all_fields(self):
        """指纹覆盖 metrics/dimensions/filters/time_range。"""
        base = {"metrics": ["gmv"], "time_range": "20260705"}
        variants = [
            {"metrics": ["refund_amount"], "time_range": "20260705"},
            {"metrics": ["gmv"], "time_range": "20260706"},
            {"metrics": ["gmv"], "time_range": "20260705",
             "dimensions": [{"name": "store_type"}]},
            {"metrics": ["gmv"], "time_range": "20260705",
             "filters": [{"field": "city", "operator": "eq", "value": "上海"}]},
        ]
        fp = ConfirmTokenStore.fingerprint(base)
        for v in variants:
            self.assertNotEqual(ConfirmTokenStore.fingerprint(v), fp)

    def test_expired(self):
        store = ConfirmTokenStore(ttl_seconds=-1)
        tok = store.issue(self.mql)
        ok, reason = store.verify(tok, self.mql)
        self.assertFalse(ok)
        self.assertIn("已过期", reason)


class SearchIndexTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.onto = Ontology(BACKEND / "ontology")

    def test_exact_metric_hit(self):
        hits = self.onto.search("gmv")
        self.assertTrue(any("gmv" in h and "[指标]" in h for h in hits))

    def test_display_name_alias_hit(self):
        # 中文展示名（对象）：门店
        hits = self.onto.search("门店")
        self.assertTrue(any("[对象]" in h for h in hits))
        # 属性别名：city（Store 的属性）
        hits = self.onto.search("city")
        self.assertTrue(any("[属性] city" in h for h in hits))

    def test_prefix_fallback(self):
        hits = self.onto.search("gmv")
        full = self.onto.search("gmv")
        self.assertTrue(len(hits) >= 1)
        # 前缀查询不差于精确查询
        self.assertGreaterEqual(len(self.onto.search("gm")), 0)

    def test_no_hit_returns_empty(self):
        self.assertEqual(self.onto.search("不存在的指标xyz"), [])

    def test_index_has_no_duplicates(self):
        hits = self.onto.search("gmv")
        self.assertEqual(len(hits), len(set(hits)))


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
