"""本体注册条目校验器测试（缺陷①③）：字段级格式校验。

覆盖验收标准：
  * pre_aggregated 传布尔 → 400 级字段错误（指明字段与期望类型）
  * required_filters 传字符串 → 400
  * object_type 枚举校验（fact|dim）
  * 合法全量条目通过
  * 合并语义（省略字段保留原值 → validator 只校验传入字段，不强制全量）

运行：backend/.venv/bin/python -m unittest tests.test_entry_validator
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from core.entry_validator import (  # noqa: E402
    EntryValidationError, validate_entry)


def _valid_object() -> dict:
    return {
        "name": "NewObj", "display_name": "新对象", "description": "测试对象",
        "domain": "ord", "object_type": "fact", "status": "active",
        "data_owner": "交易数据组", "security_level": "L2", "update_frequency": "T+1",
        "aliases": ["新对象"], "required_filters": ["is_valid = 1"], "tags": [],
        "properties": [{"name": "amount", "type": "decimal", "unit": "元",
                        "description": "金额"}],
        "source_tables": [{
            "table": "dwd_ord_new_di", "layer": "DWD", "authority": "gold",
            "joinable": True, "perm_column": "region_id",
            "granularities": ["day"], "available_dims": [],
            "field_mapping": {"amount": "amount"},
            "pre_aggregated": {"amount": "amount"},
            "status": "active", "description": "新表"}],
        "versions": [{"version": "v1.0", "status": "active", "definition": "初始"}],
        "default_version": None,
    }


def _valid_function() -> dict:
    return {
        "name": "new_metric", "display_name": "新指标", "description": "测试指标",
        "formula": "SUM(amount)", "owner": "NewObj",
        "domain": "ord", "category": "规模", "status": "active",
        "data_owner": "交易数据组", "unit": "元", "tags": [],
        "required_filters": ["is_valid = 1"], "supported_dimensions": [],
        "supported_granularities": ["day"], "family": None, "variant_label": None,
        "default_of_family": False, "do_not": "", "version": "v1.0",
    }


class EntryValidatorTest(unittest.TestCase):
    # ── 缺陷①：字段级格式校验 ──────────────────────────────
    def test_pre_aggregated_bool_rejected(self):
        """pre_aggregated 传布尔 → 字段级错误（核心验收用例）。"""
        o = _valid_object()
        o["source_tables"][0]["pre_aggregated"] = False
        with self.assertRaises(EntryValidationError) as ctx:
            validate_entry("object", o)
        msg = str(ctx.exception)
        self.assertIn("pre_aggregated", msg)
        self.assertIn("字典", msg)
        self.assertIn("布尔值", msg)

    def test_required_filters_string_rejected(self):
        """required_filters 传字符串 → 400（核心验收用例）。"""
        o = _valid_object()
        o["required_filters"] = "is_valid = 1"
        with self.assertRaises(EntryValidationError) as ctx:
            validate_entry("object", o)
        self.assertIn("required_filters", str(ctx.exception))
        self.assertIn("列表", str(ctx.exception))

    def test_field_mapping_missing_rejected(self):
        """source_tables[].field_mapping 缺失 → 必填错误。"""
        o = _valid_object()
        del o["source_tables"][0]["field_mapping"]
        with self.assertRaises(EntryValidationError) as ctx:
            validate_entry("object", o)
        self.assertIn("field_mapping", str(ctx.exception))

    def test_property_type_enum_rejected(self):
        """properties[].type 非法枚举 → 字段级错误。"""
        o = _valid_object()
        o["properties"][0]["type"] = "float"
        with self.assertRaises(EntryValidationError) as ctx:
            validate_entry("object", o)
        self.assertIn("properties[0].type", str(ctx.exception))

    def test_source_table_layer_enum_rejected(self):
        """layer 枚举含 ODS/DWD/DWS/ADS/DIM；非法层（如 XX）→ 字段级错误。"""
        o = _valid_object()
        o["source_tables"][0]["layer"] = "ODS"
        validate_entry("object", o)  # ODS 合法（与 metadata.LAYER_CN 一致）
        o["source_tables"][0]["layer"] = "XX"
        with self.assertRaises(EntryValidationError) as ctx:
            validate_entry("object", o)
        self.assertIn("layer", str(ctx.exception))

    # ── 缺陷③：object_type 枚举 ────────────────────────────
    def test_object_type_enum(self):
        """object_type 支持 fact/dim，非法值 400。"""
        o = _valid_object()
        o["object_type"] = "dim"
        validate_entry("object", o)  # dim 合法
        o["object_type"] = "cube"
        with self.assertRaises(EntryValidationError) as ctx:
            validate_entry("object", o)
        self.assertIn("object_type", str(ctx.exception))

    def test_object_type_default_fact(self):
        """省略 object_type → 补默认 fact（合并语义：省略保留原值由 DB merge 保证）。"""
        o = _valid_object()
        del o["object_type"]
        validate_entry("object", o)
        self.assertEqual(o["object_type"], "fact")

    # ── function 校验 ──────────────────────────────────────
    def test_function_missing_formula_rejected(self):
        f = _valid_function()
        del f["formula"]
        with self.assertRaises(EntryValidationError) as ctx:
            validate_entry("function", f)
        self.assertIn("formula", str(ctx.exception))

    def test_function_formula_whitelist_enforced(self):
        """formula 非法标识符（非白名单函数且非小写属性名）→ 字段级错误。"""
        f = _valid_function()
        f["formula"] = "HACK(amount)"  # HACK 非白名单函数
        with self.assertRaises(EntryValidationError) as ctx:
            validate_entry("function", f)
        self.assertIn("formula", str(ctx.exception))
        self.assertIn("HACK", str(ctx.exception))
        # 合法公式（白名单函数 + 小写属性）通过
        f["formula"] = "SUM(pay_amount) / COUNT(DISTINCT order_id)"
        validate_entry("function", f)

    def test_function_status_enum_default(self):
        f = _valid_function()
        del f["status"]
        validate_entry("function", f)
        self.assertEqual(f["status"], "active")
        f["status"] = "archived"
        with self.assertRaises(EntryValidationError) as ctx:
            validate_entry("function", f)
        self.assertIn("status", str(ctx.exception))

    def test_function_required_filters_string_rejected(self):
        f = _valid_function()
        f["required_filters"] = "is_valid = 1"
        with self.assertRaises(EntryValidationError) as ctx:
            validate_entry("function", f)
        self.assertIn("required_filters", str(ctx.exception))

    def test_function_valid_passes(self):
        validate_entry("function", _valid_function())

    # ── relation / glossary / config ───────────────────────
    def test_relation_requires_keys(self):
        with self.assertRaises(EntryValidationError):
            validate_entry("relation", {"source": "A"})  # 缺 target/join_key

    def test_relation_cardinality_enum(self):
        with self.assertRaises(EntryValidationError):
            validate_entry("relation", {"source": "A", "target": "B",
                                        "join_key": "id", "cardinality": "2:2"})

    def test_glossary_requires_type_enum(self):
        with self.assertRaises(EntryValidationError):
            validate_entry("glossary", {"term": "x", "canonical": "Y", "type": "bad"})

    def test_config_requires_key(self):
        with self.assertRaises(EntryValidationError):
            validate_entry("config", {"value": 1})

    def test_unknown_kind_rejected(self):
        with self.assertRaises(EntryValidationError) as ctx:
            validate_entry("table", {})
        self.assertIn("不支持", str(ctx.exception))

    # ── 合并语义：省略字段不强制（只校验传入字段）──────────
    def test_minimal_object_update_allowed(self):
        """已存在条目最小更新（mode=update）：只提交 name + 要改字段即可。"""
        validate_entry("object", {"name": "Region", "display_name": "区域"}, mode="update")

    def test_minimal_object_create_requires_fields(self):
        """新建（mode=create）缺必填字段 → 字段级错误。"""
        with self.assertRaises(EntryValidationError) as ctx:
            validate_entry("object", {"name": "Region"}, mode="create")
        self.assertIn("display_name", str(ctx.exception))

    def test_valid_object_passes(self):
        validate_entry("object", _valid_object())


if __name__ == "__main__":
    unittest.main()
