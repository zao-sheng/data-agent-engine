"""建模方案生成测试：变更清单 → 配对 DDL+ETL（防「2 DDL / 1 ETL」）。

运行：backend/.venv/bin/python -m unittest tests.test_modeling_plan
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from core import Ontology  # noqa: E402
from core.modeling_plan import generate_modeling_plan  # noqa: E402


class ModelingPlanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.onto = Ontology(BACKEND / "ontology")

    def test_two_create_tables_pair_ddl_etl(self):
        """用户报告的场景：2 个新增表 → 必须 2 对 DDL+ETL。"""
        r = generate_modeling_plan(self.onto, [
            {"type": "create", "obj_name": "Order", "layer": "ADS",
             "subject": "repurchase", "metrics": ["order_count"]},
            {"type": "create", "obj_name": "Payment", "layer": "ADS",
             "subject": "repurchase_pay", "metrics": ["gmv"]},
        ])
        self.assertEqual(r["errors"], [])
        self.assertTrue(r["summary"]["paired"])
        self.assertEqual(r["summary"]["ddl"], 2)
        self.assertEqual(r["summary"]["etl"], 2)
        # 每个变更都带 ddl 和 etl
        for c in r["changes"]:
            self.assertIn("ddl", c, f"变更 {c['obj_name']} 缺 DDL")
            self.assertIn("etl", c, f"变更 {c['obj_name']} 缺 ETL")
            self.assertEqual(c["pair"]["ddl_table"], c["pair"]["etl_target"])

    def test_mixed_types_paired_semantics(self):
        """新增+改逻辑+注册：paired 只看新增类。"""
        r = generate_modeling_plan(self.onto, [
            {"type": "create", "obj_name": "Order", "layer": "ADS",
             "subject": "repurchase", "metrics": ["order_count"]},
            {"type": "modify_logic", "obj_name": "Payment", "layer": "ADS",
             "subject": "gmv", "metrics": ["gmv"]},
            {"type": "register", "obj_name": "ActiveUser"},
        ])
        self.assertEqual(r["errors"], [])
        self.assertTrue(r["summary"]["paired"])
        self.assertEqual(r["summary"]["ddl"], 1)
        self.assertEqual(r["summary"]["etl"], 2)  # create 1 + modify 1
        # register 无 ddl/etl
        reg = [c for c in r["changes"] if c["type"] == "register"][0]
        self.assertNotIn("ddl", reg)
        self.assertNotIn("etl", reg)

    def test_dim_create_no_etl_required(self):
        """DIM 维表新建无需 ETL，配对仍成立。"""
        r = generate_modeling_plan(self.onto, [
            {"type": "create", "obj_name": "Store", "layer": "DIM"},
        ])
        self.assertEqual(r["errors"], [])
        self.assertTrue(r["summary"]["paired"])
        c = r["changes"][0]
        self.assertIn("ddl", c)
        self.assertNotIn("etl", c)
        self.assertIn("无聚合 ETL", c["pair"]["note"])

    def test_fact_without_etl_source_reports_error(self):
        """事实表但对象无 DWD 明细源 → 明确报错（不静默产出不完整方案）。"""
        r = generate_modeling_plan(self.onto, [
            {"type": "create", "obj_name": "ActiveUser", "layer": "DWS",
             "metrics": ["order_user_cnt"]},
        ])
        self.assertFalse(r["summary"]["paired"])
        self.assertTrue(any("ETL 失败" in e for e in r["errors"]))

    def test_editable_fields_exposed(self):
        """每个变更暴露可编辑字段，供确认环节逐项修改。"""
        r = generate_modeling_plan(self.onto, [
            {"type": "create", "obj_name": "Order", "layer": "ADS",
             "subject": "repurchase", "metrics": ["order_count"]},
        ])
        c = r["changes"][0]
        self.assertIn("obj_name", c["editable"])
        self.assertIn("layer", c["editable"])
        self.assertIn("metrics", c["editable"])
        self.assertIn("dimensions", c["editable"])
        self.assertIn("editable_fields", r["summary"])

    def test_invalid_change_type(self):
        r = generate_modeling_plan(self.onto, [
            {"type": "drop_table", "obj_name": "Order"},
        ])
        self.assertTrue(any("变更类型" in e for e in r["errors"]))

    def test_empty_changes(self):
        r = generate_modeling_plan(self.onto, [])
        self.assertIn("error", r)

    def test_missing_layer_for_create(self):
        r = generate_modeling_plan(self.onto, [
            {"type": "create", "obj_name": "Order", "metrics": ["order_count"]},
        ])
        self.assertTrue(any("缺少 layer" in e for e in r["errors"]))


class ModelingWorkflowEditabilityTest(unittest.TestCase):
    """modeling-workflow 的可编辑交互要求。"""

    SKILL = BACKEND.parent / "dsh-side" / "agent-presets" / "data-agent" \
        / "skills" / "modeling-workflow" / "SKILL.md"

    def test_editable_phase_0(self):
        text = self.SKILL.read_text(encoding="utf-8")
        self.assertIn("可编辑", text)
        self.assertIn("来源", text)          # 信息来源列
        self.assertIn("需求文档", text)
        self.assertIn("用户input", text)
        self.assertIn("待确认", text)

    def test_editable_phase_1(self):
        text = self.SKILL.read_text(encoding="utf-8")
        # 阶段 1 方案书：清单确认协议 + 可编辑
        self.assertIn("可编辑清单", text)
        self.assertIn("整体确认", text)
        self.assertIn("要修改的变更项", text)

    def test_paired_generation_phase(self):
        text = self.SKILL.read_text(encoding="utf-8")
        self.assertIn("modeling_plan", text)
        self.assertIn("配对", text)
        self.assertIn("summary.paired", text)

    def test_confirm_list_protocol_present(self):
        """清单确认协议：先输出完整清单（编号）→ 一次整体确认，禁止逐项弹选择题。"""
        text = self.SKILL.read_text(encoding="utf-8")
        self.assertIn("清单确认协议", text)
        self.assertIn("整体确认", text)
        self.assertIn("编号 + 新内容", text)
        self.assertIn("禁止", text)
        self.assertIn("多次单问选择题", text)
        self.assertIn("ask_user_question", text)

    def test_editable_list_present_in_phases(self):
        """阶段 0 需求表、阶段 1 方案书均为可编辑清单。"""
        text = self.SKILL.read_text(encoding="utf-8")
        self.assertIn("建模需求识别表（草稿，可编辑）", text)
        self.assertIn("来源", text)
        self.assertIn("用户input", text)
        self.assertIn("待确认", text)
        # 阶段 1 方案书可编辑
        self.assertIn("可编辑清单", text)


if __name__ == "__main__":
    unittest.main()
