"""路径 D 测试：Spark SQL DDL 生成 + ETL 脚本生成 + 建模流程模板完整性。

运行：backend/.venv/bin/python -m unittest tests.test_modeling
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from core import Ontology  # noqa: E402
from core.ddl_gen import generate_ddl, spark_type, table_name  # noqa: E402
from core.etl_gen import generate_etl  # noqa: E402


class DdlGenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.onto = Ontology(BACKEND / "ontology")

    def test_spark_type_mapping(self):
        self.assertEqual(spark_type("string"), "STRING")
        self.assertEqual(spark_type("decimal"), "DECIMAL(18,2)")
        self.assertEqual(spark_type("date"), "DATE")
        self.assertEqual(spark_type("int"), "INT")
        self.assertEqual(spark_type("bigint"), "BIGINT")
        self.assertEqual(spark_type("unknown"), "STRING")

    def test_table_name(self):
        self.assertEqual(table_name("DWD", "ord", "pay"), "dwd_ord_pay_di")
        self.assertEqual(table_name("DWS", "ord", "pay"), "dws_ord_pay_1d")
        self.assertEqual(table_name("ADS", "ord", "gmv"), "ads_ord_gmv_1d")
        self.assertEqual(table_name("DIM", "ord", "store"), "dim_ord_store")

    def test_fact_ddl_spark_sql(self):
        r = generate_ddl(self.onto, "Payment", "DWS", subject="pay")
        self.assertNotIn("error", r)
        ddl = r["ddl"]
        # Spark SQL 特征
        self.assertIn("CREATE TABLE IF NOT EXISTS dws_ord_pay_1d", ddl)
        self.assertIn("PARTITIONED BY (dt STRING", ddl)   # Hive 风格分区
        self.assertIn("STORED AS PARQUET", ddl)
        self.assertIn("DECIMAL(18,2)", ddl)
        # 非 Doris 特征
        self.assertNotIn("PARTITION BY RANGE", ddl)
        self.assertNotIn("VARCHAR", ddl)
        self.assertIn("engine", r)
        self.assertEqual(r["engine"], "spark")
        self.assertTrue(r["review_required"])

    def test_dim_ddl_no_partition(self):
        r = generate_ddl(self.onto, "Store", "DIM")
        ddl = r["ddl"]
        self.assertIn("dim_ord_store", ddl)
        self.assertNotIn("PARTITIONED BY", ddl)

    def test_ddl_domain_defaults_to_object_domain(self):
        """domain 缺省取对象自身业务域（本体治理字段，非硬编码 ord）。"""
        # Product 域 = prd（维度表命名 dim_prd_product）
        r = generate_ddl(self.onto, "Product", "DIM")
        self.assertNotIn("error", r)
        self.assertEqual(r["domain"], "prd")
        self.assertEqual(r["table"], "dim_prd_product")
        self.assertIn("dim_prd_product", r["ddl"])
        # Order 域 = ord（显式传 domain 仍覆盖）
        r2 = generate_ddl(self.onto, "Order", "DWS", domain="fin", subject="order")
        self.assertEqual(r2["table"], "dws_fin_order_1d")

    def test_ddl_unknown_object(self):
        r = generate_ddl(self.onto, "NotExist", "DWD")
        self.assertIn("error", r)


class EtlGenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.onto = Ontology(BACKEND / "ontology")

    def test_basic_etl_spark_sql(self):
        r = generate_etl(self.onto, "Payment", "ADS", subject="gmv", metrics=["gmv"])
        self.assertNotIn("error", r)
        etl = r["etl"]
        self.assertIn("INSERT OVERWRITE TABLE ads_ord_gmv_1d", etl)
        self.assertIn("PARTITION (dt)", etl)
        self.assertIn("FROM dwd_ord_pay_di F", etl)
        self.assertIn("SUM(F.pay_amt) AS gmv", etl)   # 公式编译
        self.assertIn("WHERE F.is_valid = 1", etl)    # required_filter 注入
        self.assertIn("GROUP BY F.dt", etl)
        self.assertEqual(r["engine"], "spark")
        self.assertTrue(r["review_required"])

    def test_etl_domain_defaults_to_object_domain(self):
        """ETL domain 缺省取对象域；deprecated 源表被跳过。"""
        r = generate_etl(self.onto, "Payment", "ADS", subject="gmv", metrics=["gmv"])
        self.assertNotIn("error", r)
        self.assertEqual(r["target"], "ads_ord_gmv_1d")
        # 显式传 domain 覆盖
        r2 = generate_etl(self.onto, "Order", "DWS", domain="fin", subject="order",
                          metrics=["order_count"])
        self.assertNotIn("error", r2)
        self.assertEqual(r2["target"], "dws_fin_order_1d")

    def test_etl_with_dimension(self):
        r = generate_etl(self.onto, "Payment", "DWS", subject="pay",
                         metrics=["gmv", "pay_count"], dimensions=["channel"])
        etl = r["etl"]
        self.assertIn("COUNT(F.pay_id) AS pay_count", etl)
        self.assertIn("F.channel AS channel", etl)
        self.assertIn("GROUP BY F.dt, F.channel", etl)

    def test_etl_cross_domain_metrics_require_cte(self):
        # gmv(Payment) + refund_amount(Refund) 跨域 → 应报错或提示（当前单源语义）
        r = generate_etl(self.onto, "Payment", "ADS", subject="gmv",
                         metrics=["gmv", "refund_amount"])
        # 当前实现是单源聚合：跨 owner 指标不在源表 field_mapping，应给出明确错误
        self.assertIn("error", r)

    def test_etl_unknown_metric(self):
        r = generate_etl(self.onto, "Payment", "ADS", subject="gmv", metrics=["nope"])
        self.assertIn("error", r)

    def test_etl_no_detail_source(self):
        # Store 是 DIM，无 DWD 明细 → 无法生成 ETL
        r = generate_etl(self.onto, "Store", "DIM", subject="store", metrics=["gmv"])
        self.assertIn("error", r)


class ModelingTemplateTest(unittest.TestCase):
    """modeling-workflow skill 模板完整性检查（关键章节存在性）。"""

    SKILL = BACKEND.parent / "dsh-side" / "agent-presets" / "data-agent" / "skills" \
        / "modeling-workflow" / "SKILL.md"

    def test_skill_exists_and_has_phases(self):
        self.assertTrue(self.SKILL.exists(), "modeling-workflow SKILL.md 缺失")
        text = self.SKILL.read_text(encoding="utf-8")
        # 阶段 2/3 已合并为「阶段 2/3：方案落地」，不再有独立「阶段 3」
        for phase in ["阶段 0", "阶段 1", "阶段 2/3", "阶段 4",
                      "阶段 5", "阶段 6", "阶段 7"]:
            self.assertIn(phase, text, f"缺少 {phase}")

    def test_templates_present(self):
        text = self.SKILL.read_text(encoding="utf-8")
        self.assertIn("需求识别提取模板", text)
        self.assertIn("建模方案书", text)
        self.assertIn("模型变更评估", text)

    def test_confirm_before_execute_principle(self):
        text = self.SKILL.read_text(encoding="utf-8")
        self.assertIn("先产出逻辑报告", text)
        self.assertIn("确认后", text)


if __name__ == "__main__":
    unittest.main()
