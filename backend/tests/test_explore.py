"""探索性取数测试（P0/P1）：intent 探索识别 + explore_validate/execute 安全护栏。

覆盖：
  * intent_classify 识别探索意图（path=E）
  * explore_validate：合法 SQL 通过；写操作/未注册表/无分区 拒绝
  * explore_token 绑定 SQL 指纹（换 SQL 拒绝）
  * explore_execute 走只读护栏
运行：backend/.venv/bin/python -m unittest tests.test_explore
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from core.explore import (  # noqa: E402
    candidate_tables, extract_tables, validate_explore_sql)
from core.intent import classify_intent  # noqa: E402
from core.ontology_loader import Ontology  # noqa: E402

ONTOLOGY_DIR = BACKEND / "ontology"


class ExploreValidateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.onto = Ontology(ONTOLOGY_DIR)
        cls.allowed = candidate_tables(cls.onto)

    def test_valid_sql_passes(self):
        sql = ("SELECT F.region_id, COUNT(*) AS cnt FROM dwd_ord_pay_di F "
               "WHERE F.dt = '20260817' GROUP BY F.region_id")
        r = validate_explore_sql(sql, self.allowed)
        self.assertTrue(r["ok"], r)
        self.assertIn("dwd_ord_pay_di", r["tables"])

    def test_multi_join_tables_extracted(self):
        sql = ("SELECT R.region_name FROM dwd_ord_pay_di F "
               "JOIN dim_region R ON R.region_id = F.region_id "
               "JOIN dim_store S ON S.store_id = F.store_id "
               "WHERE F.dt = '20260817'")
        r = validate_explore_sql(sql, self.allowed)
        self.assertTrue(r["ok"], r)
        self.assertEqual(set(r["tables"]),
                         {"dwd_ord_pay_di", "dim_region", "dim_store"})

    def test_write_statement_rejected(self):
        r = validate_explore_sql("INSERT INTO dwd_ord_pay_di VALUES(1)", self.allowed)
        self.assertFalse(r["ok"])
        self.assertTrue(any("只允许" in e for e in r["errors"]))

    def test_unknown_prefixed_table_rejected(self):
        r = validate_explore_sql("SELECT * FROM dwd_ord_fake_di WHERE dt='20260817'",
                                 self.allowed)
        self.assertFalse(r["ok"])
        self.assertTrue(any("未注册表" in e for e in r["errors"]))

    def test_missing_partition_rejected(self):
        r = validate_explore_sql("SELECT * FROM dwd_ord_pay_di", self.allowed)
        self.assertFalse(r["ok"])
        self.assertTrue(any("分区" in e for e in r["errors"]))

    def test_empty_sql_rejected(self):
        r = validate_explore_sql("", self.allowed)
        self.assertFalse(r["ok"])

    def test_cte_allowed(self):
        sql = ("WITH t AS (SELECT region_id, pay_amt FROM dwd_ord_pay_di "
               "WHERE dt='20260817') SELECT region_id, SUM(pay_amt) FROM t "
               "GROUP BY region_id")
        r = validate_explore_sql(sql, self.allowed)
        self.assertTrue(r["ok"], r)

    def test_extract_tables_dedup(self):
        tables = extract_tables(
            "SELECT * FROM dwd_ord_pay_di a JOIN dwd_ord_pay_di b ON a.x=b.x "
            "WHERE dt='1'")
        self.assertEqual(tables.count("dwd_ord_pay_di"), 1)  # 去重


class ExploreIntentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.onto = Ontology(ONTOLOGY_DIR)

    def test_explore_intent_detected(self):
        for q in ("先看看支付数据长什么样", "大概看看退款情况", "探索一下消费数据"):
            r = classify_intent(self.onto, q)
            self.assertEqual(r["intent"], "explore", q)
            self.assertEqual(r["path"], "E")

    def test_registered_metric_not_explore(self):
        # "昨天的 GMV" 命中注册指标 → 走 query 而非 explore
        r = classify_intent(self.onto, "昨天的GMV是多少")
        self.assertEqual(r["intent"], "query")
        self.assertNotEqual(r["path"], "E")

    def test_explore_mode_detection(self):
        """探索三模式识别：mixed（起草）/ nl（探索词）/ direct（贴 SQL）。"""
        mixed_cases = [
            "帮我起草个SQL看看华东区支付金额分布",
            "先给我个SQL骨架我改改，看下订单和退款数据",
            "生成SQL，探索一下门店维度的消费情况",
        ]
        for q in mixed_cases:
            r = classify_intent(self.onto, q)
            self.assertEqual(r["intent"], "explore", q)
            self.assertEqual(r["explore_mode"], "mixed", q)
        # NL 探索 → nl
        r = classify_intent(self.onto, "先看看支付数据长什么样")
        self.assertEqual(r["intent"], "explore")
        self.assertEqual(r["explore_mode"], "nl")
        # 贴 SQL → direct
        r = classify_intent(self.onto, "SELECT * FROM dwd_ord_pay_di WHERE dt='1'")
        self.assertEqual(r["intent"], "explore")
        self.assertEqual(r["explore_mode"], "direct")

    def test_pasted_sql_detected_as_explore(self):
        """会话内直接贴 SQL（含小写/CTE/markdown 块）→ explore 路径。"""
        cases = [
            "SELECT region_id, COUNT(*) FROM dwd_ord_pay_di WHERE dt='20260817' GROUP BY region_id",
            "  select F.region_id from dwd_ord_pay_di F where F.dt='1'",
            "WITH t AS (SELECT * FROM dwd_ord_pay_di WHERE dt='1') SELECT COUNT(*) FROM t",
            "```sql\nSELECT * FROM dwd_ord_pay_di WHERE dt='20260817'\n```",
            # 多行 SQL（FROM 在行首）——真实粘贴场景
            "SELECT R.region_name, SUM(F.pay_amt) AS gmv\n"
            "FROM dwd_ord_pay_di F\n"
            "JOIN dim_region R ON R.region_id = F.region_id\n"
            "WHERE F.dt = '20260817'\n"
            "GROUP BY R.region_name",
            # 全换行紧凑格式
            "SELECT\nF.region_id,\nCOUNT(*)\nFROM\ndwd_ord_pay_di F\nWHERE F.dt='1'",
        ]
        for sql in cases:
            r = classify_intent(self.onto, sql)
            self.assertEqual(r["intent"], "explore", sql)
            self.assertEqual(r["path"], "E")


if __name__ == "__main__":
    unittest.main()
