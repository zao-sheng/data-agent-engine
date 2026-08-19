"""意图识别测试（plan 层）：四类意图 / 混合意图 / 低置信澄清 / 词表误伤防护 / 证据链。

运行：backend/.venv/bin/python -m unittest tests.test_intent
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from core import Ontology  # noqa: E402
from core.intent import classify_intent  # noqa: E402


class IntentClassifyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.onto = Ontology(BACKEND / "ontology")

    def classify(self, text: str) -> dict:
        return classify_intent(self.onto, text)

    # ── 四类意图 ──────────────────────────────────────────────
    def test_query_with_metric_and_time(self):
        r = self.classify("华东区数码类产品最近 30 天的 GMV，按城市拆分")
        self.assertEqual(r["intent"], "query")
        self.assertEqual(r["path"], "A")
        self.assertIn("gmv", r["metric_hits"])
        self.assertFalse(r["metrics_missing"])

    def test_query_metric_only(self):
        r = self.classify("昨天的 GMV 是多少")
        self.assertEqual(r["intent"], "query")
        self.assertEqual(r["confidence"], "high")

    def test_metadata_how_to_calculate(self):
        r = self.classify("GMV 怎么算的")
        self.assertEqual(r["intent"], "metadata")
        self.assertEqual(r["path"], "metadata")
        self.assertTrue(any(e["signal"] == "metadata" for e in r["evidence"]))

    def test_metadata_table_lineage(self):
        r = self.classify("ads_ord_gmv_1d 这张表是怎么来的")
        self.assertEqual(r["intent"], "metadata")

    def test_metadata_definition(self):
        r = self.classify("GMV 是什么")
        self.assertEqual(r["intent"], "metadata")

    def test_modeling_new_table(self):
        r = self.classify("帮我建一个用户复购率表")
        self.assertEqual(r["intent"], "modeling")
        self.assertEqual(r["path"], "D")

    def test_modeling_add_field(self):
        r = self.classify("给订单表加个字段")
        self.assertEqual(r["intent"], "modeling")

    def test_operation_change_schedule(self):
        r = self.classify("把 gmv 的 ETL 调度改到早上 8 点")
        self.assertEqual(r["intent"], "operation")
        self.assertEqual(r["path"], "direct")

    def test_operation_pause_task(self):
        r = self.classify("暂停 gmv 的 ETL 任务")
        self.assertEqual(r["intent"], "operation")

    # ── 低置信 / 澄清 ─────────────────────────────────────────
    def test_unclear_no_signal(self):
        r = self.classify("你好")
        self.assertEqual(r["intent"], "unclear")
        self.assertEqual(r["path"], "clarify")
        self.assertEqual(r["confidence"], "low")

    def test_operation_without_object_is_unclear(self):
        """裸修改动词但无存量对象提及 → 澄清，不猜路径。"""
        r = self.classify("帮我改一下")
        self.assertEqual(r["intent"], "unclear")

    # ── 混合意图 ──────────────────────────────────────────────
    def test_mixed_metadata_then_query(self):
        r = self.classify("GMV 怎么算的？顺便拉一下最近 30 天")
        self.assertEqual(r["intent"], "metadata")
        self.assertEqual([m["intent"] for m in r["mixed"]], ["query"])

    def test_time_context_only_not_mixed(self):
        """「昨天的 GMV 怎么算的」仍是口径咨询，纯时间词不构成混合意图。"""
        r = self.classify("昨天的 GMV 怎么算的")
        self.assertEqual(r["intent"], "metadata")
        self.assertEqual(r["mixed"], [])

    # ── 黑话 / 词表误伤防护 ───────────────────────────────────
    def test_slang_metric_hit(self):
        r = self.classify("成交额是多少")
        self.assertEqual(r["intent"], "query")
        self.assertIn("gmv", r["metric_hits"])   # 归一后命中标准指标

    def test_advice_word_not_modeling(self):
        """「建议」含「建」但不在建模动词表 → 不误判建模。"""
        r = self.classify("建议按周看 GMV")
        self.assertEqual(r["intent"], "query")

    def test_query_missing_metric(self):
        r = self.classify("最近 30 天卖得怎么样")
        self.assertEqual(r["intent"], "query")
        self.assertTrue(r["metrics_missing"])

    # ── 结构与联动 ────────────────────────────────────────────
    def test_evidence_structure(self):
        r = self.classify("GMV 怎么算的")
        self.assertTrue(r["evidence"])
        for e in r["evidence"]:
            self.assertIn("type", e)
            self.assertIn("value", e)
            self.assertIn("signal", e)

    def test_path_mapping_all_intents(self):
        cases = [
            ("昨天的 GMV 是多少", "A"),
            ("GMV 怎么算的", "metadata"),
            ("帮我建一个表", "D"),
            ("把 gmv 的 ETL 调度改到早上 8 点", "direct"),
            ("你好", "clarify"),
        ]
        for text, path in cases:
            self.assertEqual(self.classify(text)["path"], path, text)

    def test_normalized_text_returned(self):
        r = self.classify("poi 的成交额是多少")
        self.assertEqual(r["intent"], "query")
        self.assertIn("gmv", r["normalized_text"].lower())


if __name__ == "__main__":
    unittest.main()
