"""探索固化桥接测试（P2）：探索 SQL → 口径提取 → 注册草稿。

覆盖：
  * 聚合表达式 → 候选指标（formula/owner 反查 field_mapping）
  * GROUP BY → 维度；WHERE 非 dt → required_filters
  * 未注册表 → object 草稿；未映射列 → unmapped_columns
  * 草稿可编辑、status=draft（不自动激活）
运行：backend/.venv/bin/python -m unittest tests.test_explore_promote
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from core.explore_promote import (  # noqa: E402
    _extract_group_dims, _extract_select_metrics, _extract_where_filters,
    _resolve_column, build_register_draft, extract_metric_definition)
from core.ontology_loader import Ontology  # noqa: E402

ONTOLOGY_DIR = BACKEND / "ontology"

EXPLORE_SQL = """SELECT S.store_type,
               SUM(F.pay_amt) AS gmv,
               COUNT(DISTINCT F.order_id) AS order_cnt
        FROM dwd_ord_pay_di F
        JOIN dim_store S ON S.store_id = F.store_id
        WHERE F.is_valid = 1 AND F.dt BETWEEN '20260801' AND '20260817'
        GROUP BY S.store_type"""


class ExtractMetricTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.onto = Ontology(ONTOLOGY_DIR)

    def test_select_metrics_extracted(self):
        ms = _extract_select_metrics(EXPLORE_SQL)
        self.assertEqual(len(ms), 2)
        self.assertEqual(ms[0]["formula"], "SUM(pay_amt)")  # 提取层保留物理列
        self.assertEqual(ms[1]["formula"], "COUNT(order_id)")

    def test_group_dims_extracted(self):
        dims = _extract_group_dims(EXPLORE_SQL)
        self.assertEqual(dims, ["store_type"])

    def test_where_filters_exclude_dt(self):
        filters = _extract_where_filters(EXPLORE_SQL)
        self.assertEqual(filters, ["is_valid = 1"])  # dt 条件被排除；表别名前缀已去

    def test_column_resolved_via_field_mapping(self):
        info = _resolve_column(self.onto, "pay_amt")
        self.assertEqual(info["owner_object"], "Payment")
        self.assertEqual(info["property_name"], "pay_amount")
        # 未映射列
        info2 = _resolve_column(self.onto, "not_a_col")
        self.assertIsNone(info2["owner_object"])

    def test_extract_metric_definition_full(self):
        ex = extract_metric_definition(self.onto, EXPLORE_SQL)
        self.assertEqual(len(ex["metrics"]), 2)
        self.assertEqual(ex["dimensions"], ["store_type"])
        self.assertEqual(ex["required_filters"], ["is_valid = 1"])
        self.assertEqual(set(ex["source_tables"]),
                         {"dwd_ord_pay_di", "dim_store"})
        self.assertEqual(ex["unmapped_columns"], [])


class BuildRegisterDraftTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.onto = Ontology(ONTOLOGY_DIR)

    def test_draft_has_function_changes(self):
        r = build_register_draft(self.onto, EXPLORE_SQL)
        self.assertTrue(r["ok"], r)
        fns = [c for c in r["changes"] if c.get("kind") == "function"]
        self.assertEqual(len(fns), 2)
        # 草稿不自动激活：status=draft
        self.assertTrue(all(c["entry"]["status"] == "draft" for c in fns))
        # 指标 owner 已反查
        self.assertEqual(fns[0]["entry"]["owner"], "Payment")
        self.assertEqual(fns[0]["entry"]["formula"], "SUM(pay_amount)")  # 草稿用业务属性
        # 维度进入 supported_dimensions
        self.assertIn("store_type", fns[0]["entry"]["supported_dimensions"])
        # 可编辑字段
        self.assertIn("formula", fns[0]["editable"])

    def test_unregistered_table_creates_object_draft(self):
        sql = "SELECT COUNT(*) FROM dwd_ord_new_di WHERE dt='20260817'"
        r = build_register_draft(self.onto, sql)
        objs = [c for c in r["changes"] if c.get("kind") == "object"]
        self.assertTrue(any("dwd_ord_new_di" in c["obj_name"] for c in objs))

    def test_unmapped_columns_reported(self):
        sql = ("SELECT mysterious_col, COUNT(*) FROM dwd_ord_pay_di "
               "WHERE dt='20260817' GROUP BY mysterious_col")
        r = build_register_draft(self.onto, sql)
        self.assertIn("mysterious_col", r["extracted"]["unmapped_columns"])

    def test_empty_sql_error(self):
        r = build_register_draft(self.onto, "   ")
        self.assertFalse(r["ok"])
        self.assertTrue(r["errors"])


if __name__ == "__main__":
    unittest.main()
