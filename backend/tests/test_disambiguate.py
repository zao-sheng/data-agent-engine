"""metric_disambiguate 量纲感知测试（防跨量纲误替）。

背景（用户复盘）：查「消费订单数」（计数）时，agent 被误导返回
「消费金额 consume_gmv」（金额）作为候选——跨量纲（单数 vs 金额）
根本不可替代，选项显得弱智。本次修复：
  * 查询含量纲信号（订单/单/数量 → count；金额/多少钱 → amount）
  * 命中指标量纲不符 → 不返回 exact，降级为 candidates 并标注 dimension_mismatch
  * candidates 优先「同量纲 + 语义相关」

运行：backend/.venv/bin/python -m unittest tests.test_disambiguate
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from mcp_servers import server as srv  # noqa: E402


class MetricDisambiguateTest(unittest.TestCase):
    # ── 跨量纲：计数查询不返回金额指标 ─────────────────────
    def test_consume_order_count_not_amount(self):
        """查「消费订单」（计数）→ 不得命中金额指标 consume_gmv。"""
        r = srv.metric_disambiguate("消费订单")
        self.assertEqual(r["status"], "none", r)
        self.assertIsNone(r.get("exact"), "跨量纲命中应降级")
        self.assertIn("dimension_mismatch", r)
        self.assertNotIn("consume_gmv", r.get("candidates", []))
        self.assertEqual(r["query_dimension"], "count")

    def test_refund_order_matches_count_metric(self):
        """查「退款订单」（计数）→ 候选含 refund_count（计数），不含金额指标。"""
        r = srv.metric_disambiguate("退款订单")
        cands = r.get("candidates", [])
        self.assertIn("refund_count", cands)
        self.assertNotIn("refund_amount", cands)  # 金额指标不出现

    def test_pay_order_includes_pay_count(self):
        r = srv.metric_disambiguate("支付订单")
        self.assertIn("pay_count", r.get("candidates", []))

    # ── 同量纲：金额查询正常命中 ───────────────────────────
    def test_amount_query_hits_amount_metric(self):
        r = srv.metric_disambiguate("消费金额")
        self.assertEqual(r["status"], "exact")
        self.assertEqual(r["exact"]["name"], "consume_gmv")
        self.assertEqual(r["exact"]["metric_type"], "amount")

    def test_gmv_hits_amount(self):
        r = srv.metric_disambiguate("GMV")
        self.assertIn("gmv", (r.get("exact") or {}).get("name", ""))

    # ── 无量纲信号：不误伤 ─────────────────────────────────
    def test_no_dimension_signal_gives_all_candidates(self):
        r = srv.metric_disambiguate("退款情况")
        self.assertIsNone(r.get("query_dimension"))
        self.assertIn("candidates", r)

    def test_metric_type_field_present(self):
        """结果带 metric_type（count/amount），供 agent 判断量纲。"""
        r = srv.metric_disambiguate("消费金额")
        self.assertEqual(r["exact"]["metric_type"], "amount")


if __name__ == "__main__":
    unittest.main()
