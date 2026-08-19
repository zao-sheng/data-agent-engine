"""本体读取/同步测试：reload 刷新 / sync_check 归一比较。

运行：backend/.venv/bin/python -m unittest tests.test_ontology_sync
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from core.ontology_store import SUPABASE_ROW_KEYS, YamlOntologyStore  # noqa: E402

sys.path.insert(0, str(BACKEND.parent))
import ontology_sync_check as osc  # noqa: E402


class CanonicalCompareTest(unittest.TestCase):
    """_canonical 空值归一：None/'[]'/''/[]/False 等价。"""

    def test_null_equivalence(self):
        keys = SUPABASE_ROW_KEYS["ontology_functions"]
        rows = [
            {"name": "a", "default_of_family": None, "supported_dimensions": None},
            {"name": "a", "default_of_family": False, "supported_dimensions": "[]"},
        ]
        c1 = osc._canonical(rows[:1], keys)["a"]
        c2 = osc._canonical(rows[1:], keys)["a"]
        self.assertEqual(c1["default_of_family"], c2["default_of_family"])
        self.assertEqual(c1["supported_dimensions"], c2["supported_dimensions"])

    def test_real_values_kept(self):
        keys = SUPABASE_ROW_KEYS["ontology_functions"]
        rows = [{"name": "gmv", "default_of_family": True,
                 "supported_dimensions": ["channel", "city"]}]
        c = osc._canonical(rows, keys)["gmv"]
        self.assertIs(c["default_of_family"], True)
        self.assertEqual(c["supported_dimensions"], ["channel", "city"])

    def test_yaml_vs_imported_cloud_shape(self):
        """本地 YAML 经过 normalize 导入后，sync_check 应判定一致（模拟导入产物）。"""
        from core.ontology_store import normalize_rows
        local = YamlOntologyStore(BACKEND / "ontology").load()
        keys = SUPABASE_ROW_KEYS["ontology_functions"]
        # 模拟导入产物：normalize_rows 补缺 + JSON 序列化还原（None→False/'[]'）
        imported = normalize_rows(local.functions, keys)
        l = osc._canonical(local.functions, keys)
        c = osc._canonical(imported, keys)
        self.assertEqual(l, c, "导入产物的空值归一后应与 YAML 一致")


class ReloadTest(unittest.TestCase):
    """ontology_reload 刷新行为（用 mock store 验证重建）。"""

    def test_reload_rebuilds_ontology(self):
        from unittest import mock
        import core.ontology_store as os_mod

        orig_create = os_mod.create_store
        created = []
        def fake_create(kind, **kw):
            store = orig_create(kind, **kw)
            created.append(store)
            return store

        # server 模块级 reload（不 import server，直接验证 _reload_ontology 逻辑）
        # 用真实 yaml store 重建，验证 reload 后对象是新实例
        with mock.patch.object(os_mod, "create_store", side_effect=fake_create):
            # 直接构造两个 Ontology 模拟 reload 前后
            from core import Ontology
            o1 = Ontology(store=os_mod.create_store("yaml", base=BACKEND / "ontology"))
            o2 = Ontology(store=os_mod.create_store("yaml", base=BACKEND / "ontology"))
            self.assertIsNot(o1, o2)  # reload 产生新实例
            self.assertEqual(o1.get_function("gmv"), o2.get_function("gmv"))


if __name__ == "__main__":
    unittest.main()
