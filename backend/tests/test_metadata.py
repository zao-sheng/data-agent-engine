"""元数据检索测试（元数据咨询意图）：表/指标/口径/血缘/就绪时间。

运行：backend/.venv/bin/python -m unittest tests.test_metadata
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from core import Ontology  # noqa: E402
from core.metadata import MetadataService  # noqa: E402


class MetadataSearchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.onto = Ontology(BACKEND / "ontology")
        cls.meta = MetadataService(cls.onto, db_path=str(BACKEND / "seed" / "sample.db"))

    def test_table_info_exact(self):
        info = self.meta.table_info("ads_ord_gmv_1d")
        self.assertIsNotNone(info)
        self.assertEqual(info["layer"], "ADS")
        self.assertEqual(info["layer_cn"], "应用层")
        self.assertIn("gmv", info["pre_aggregated"])

    def test_table_info_fields(self):
        info = self.meta.table_info("dwd_ord_pay_di")
        self.assertIsNotNone(info)
        fields = {f["name"] for f in info["fields"]}
        self.assertIn("pay_amount", fields)
        self.assertIn("channel", fields)
        # 字段带物理列与描述
        pay = [f for f in info["fields"] if f["name"] == "pay_amount"][0]
        self.assertEqual(pay["column"], "pay_amt")

    def test_find_tables_by_keyword(self):
        hits = self.meta.find_tables("支付")
        tables = {t["table"] for t in hits}
        self.assertIn("dwd_ord_pay_di", tables)
        self.assertIn("dws_ord_pay_1d", tables)

    def test_metric_info_caliber(self):
        info = self.meta.metric_info("gmv")
        self.assertIsNotNone(info)
        self.assertEqual(info["formula"], "SUM(pay_amount)")
        self.assertEqual(info["required_filters"], ["is_valid = 1"])
        self.assertEqual(info["version"], "v1.0")
        self.assertEqual(info["family"], "gmv")

    def test_find_metrics_family(self):
        hits = self.meta.find_metrics("gmv")
        names = {m["name"] for m in hits}
        self.assertIn("gmv", names)
        self.assertIn("order_gmv", names)   # 族变体
        self.assertIn("consume_gmv", names)

    def test_lineage(self):
        lin = self.meta.lineage("ads_ord_gmv_1d")
        self.assertIsNotNone(lin)
        self.assertEqual(lin["source"], "dwd_ord_pay_di")
        self.assertIn("GMV", lin["logic"])

    def test_lineage_dwd_has_no_aggregation(self):
        lin = self.meta.lineage("dwd_ord_pay_di")
        self.assertIsNotNone(lin)
        self.assertIn("无聚合加工", lin["logic"])

    def test_readiness(self):
        r = self.meta.readiness("ads_ord_gmv_1d")
        self.assertEqual(r["table"], "ads_ord_gmv_1d")
        self.assertIn("t-1", r["ready_partition"])

    def test_search_combined(self):
        r = self.meta.search("ads_ord_gmv_1d")
        self.assertEqual(r["query"], "ads_ord_gmv_1d")
        self.assertTrue(r["tables"])
        self.assertIsNotNone(r["lineage"])
        self.assertIsNotNone(r["readiness"])

    def test_search_no_hit_gives_candidates(self):
        r = self.meta.search("复购率")
        self.assertEqual(r["tables"], [])
        self.assertEqual(r["metrics"], [])
        self.assertIn("候选表", r["note"])

    def test_search_no_hit_readiness_is_empty_dict_not_none(self):
        """缺陷②回归：无命中时 readiness 返回空 dict（ready_partition=''），
        绝不返回 None（避免下游 join 序列混入 NoneType 崩溃）。"""
        r = self.meta.search("退款 就绪时间 数据范围")
        self.assertIsInstance(r["readiness"], dict)
        self.assertEqual(r["readiness"].get("ready_partition", ""), "")
        self.assertIn("ready_partition", r["readiness"])

    def test_all_tables(self):
        tables = self.meta.all_tables()
        self.assertGreaterEqual(len(tables), 10)
        self.assertEqual(len(tables), len(set(tables)))


if __name__ == "__main__":
    unittest.main()
