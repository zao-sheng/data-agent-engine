"""本体存储测试（1A/1B）：store 抽象 / YAML↔SQLite 一致性 / 编译工具 / 回退。

运行：backend/.venv/bin/python -m unittest tests.test_ontology_store
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from core import Ontology  # noqa: E402
from core.ontology_store import (  # noqa: E402
    SqliteOntologyStore, YamlOntologyStore, compile_to_sqlite, create_store)

ONTOLOGY_DIR = BACKEND / "ontology"


class StoreConsistencyTest(unittest.TestCase):
    def test_yaml_vs_sqlite_data_identical(self):
        """YAML 与 SQLite 加载的原始数据逐条一致（按 name 对齐）。"""
        y = YamlOntologyStore(ONTOLOGY_DIR).load()
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "onto.db"
            compile_to_sqlite(y, db)
            s = SqliteOntologyStore(db).load()

            self.assertEqual({o["name"] for o in y.objects},
                             {o["name"] for o in s.objects})
            self.assertEqual({f["name"] for f in y.functions},
                             {f["name"] for f in s.functions})
            self.assertEqual(y.relations, s.relations)
            self.assertEqual(y.glossary, s.glossary)
            self.assertEqual(y.config, s.config)

            y_obj = {o["name"]: o for o in y.objects}
            s_obj = {o["name"]: o for o in s.objects}
            for name in y_obj:
                self.assertEqual(y_obj[name], s_obj[name], f"对象 {name} 不一致")

            y_fn = {f["name"]: f for f in y.functions}
            s_fn = {f["name"]: f for f in s.functions}
            for name in y_fn:
                self.assertEqual(y_fn[name], s_fn[name], f"指标 {name} 不一致")

    def test_ontology_behavior_identical_across_stores(self):
        """用 SqliteStore 构建的 Ontology 行为与 YAML 一致（索引/查询）。"""
        y_onto = Ontology(ONTOLOGY_DIR)
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "onto.db"
            compile_to_sqlite(YamlOntologyStore(ONTOLOGY_DIR).load(), db)
            s_onto = Ontology(store=SqliteOntologyStore(db))

            self.assertEqual(len(y_onto.objects), len(s_onto.objects))
            self.assertEqual(len(y_onto.functions), len(s_onto.functions))
            self.assertEqual(y_onto.time_dim, s_onto.time_dim)
            self.assertEqual(y_onto.partition_col, s_onto.partition_col)
            # 关键查询行为一致（search 结果按集合比较：加载顺序不影响内容）
            self.assertEqual(y_onto.get_function("gmv"), s_onto.get_function("gmv"))
            self.assertEqual(set(y_onto.search("gmv")), set(s_onto.search("gmv")))
            self.assertEqual(y_onto.normalize_terms("poi 的 goods"),
                             s_onto.normalize_terms("poi 的 goods"))
            # JOIN 可达性
            self.assertEqual(y_onto.join_path("Order", "Region"),
                             s_onto.join_path("Order", "Region"))


class CompileToolTest(unittest.TestCase):
    def test_compile_idempotent(self):
        """重复编译结果一致（清空重写）。"""
        y = YamlOntologyStore(ONTOLOGY_DIR).load()
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "onto.db"
            compile_to_sqlite(y, db)
            s1 = SqliteOntologyStore(db).load()
            compile_to_sqlite(y, db)  # 第二次编译
            s2 = SqliteOntologyStore(db).load()
            self.assertEqual(s1.objects, s2.objects)
            self.assertEqual(s1.functions, s2.functions)

    def test_compile_records_meta(self):
        y = YamlOntologyStore(ONTOLOGY_DIR).load()
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "onto.db"
            compile_to_sqlite(y, db, source_commit="abc123")
            import sqlite3
            conn = sqlite3.connect(db)
            meta = dict(conn.execute("SELECT key, value FROM ontology_meta").fetchall())
            conn.close()
            self.assertEqual(meta["schema_version"], "1")
            self.assertEqual(meta["source_commit"], "abc123")
            self.assertIn("compiled_at", meta)


class SqliteStoreFallbackTest(unittest.TestCase):
    def test_missing_db_raises_clear_error(self):
        store = SqliteOntologyStore("/nonexistent/onto.db")
        with self.assertRaises(FileNotFoundError) as ctx:
            store.load()
        self.assertIn("ontology_compile", str(ctx.exception))


class StoreFactoryTest(unittest.TestCase):
    def test_create_yaml_store(self):
        s = create_store("yaml", base=str(ONTOLOGY_DIR))
        self.assertIsInstance(s, YamlOntologyStore)

    def test_create_sqlite_store(self):
        s = create_store("sqlite", db="/tmp/whatever.db")
        self.assertIsInstance(s, SqliteOntologyStore)

    def test_create_unsupported(self):
        with self.assertRaises(ValueError):
            create_store("postgres", base=str(ONTOLOGY_DIR))

    def test_create_yaml_requires_base(self):
        with self.assertRaises(ValueError):
            create_store("yaml")

    def test_create_sqlite_requires_db(self):
        with self.assertRaises(ValueError):
            create_store("sqlite")


class YamlStoreEdgeTest(unittest.TestCase):
    def test_missing_glossary_tolerated(self):
        """glossary.yaml 缺失不应报错（空黑话表）。"""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            # 只写核心三件套
            (d / "objects.yaml").write_text("objects: []", encoding="utf-8")
            (d / "functions.yaml").write_text("functions: []", encoding="utf-8")
            (d / "relations.yaml").write_text("relations: []", encoding="utf-8")
            store = YamlOntologyStore(d)
            data = store.load()
            self.assertEqual(data.glossary, [])
            self.assertEqual(data.config, {})


class EnrichedOntologyTest(unittest.TestCase):
    """本体丰富化（域路由 / 对象类型 / 关系语义 / 状态感知检索）。"""

    @classmethod
    def setUpClass(cls):
        cls.onto = Ontology(ONTOLOGY_DIR)

    def test_domain_index_routes_cross_domain(self):
        """业务域索引：跨域查询按域过滤候选集。"""
        self.assertIn("ord", cls := self.onto.domains())
        self.assertIn("usr", self.onto.domains())
        # ord 域包含事实与维度对象
        ord_objs = self.onto.objects_by_domain("ord")
        self.assertIn("Order", ord_objs)
        self.assertIn("Payment", ord_objs)
        self.assertIn("Store", ord_objs)      # 门店维度属 ord
        self.assertNotIn("ActiveUser", ord_objs)  # 用户维度属 usr
        self.assertEqual(self.onto.domain_of("Order"), "ord")
        self.assertEqual(self.onto.domain_of("ActiveUser"), "usr")

    def test_object_type_index(self):
        """对象类型索引：fact/dim 分离。"""
        facts = self.onto.objects_of_type("fact")
        dims = self.onto.objects_of_type("dim")
        self.assertIn("Order", facts)
        self.assertIn("Payment", facts)
        self.assertIn("Product", dims)
        self.assertIn("Store", dims)
        self.assertNotIn("Order", dims)
        self.assertNotIn("Product", facts)

    def test_domain_fallback_inference_from_table_name(self):
        """缺省 domain 由表名推断（dwd_<domain>_<subject>_di）。"""
        # 构造无 domain 的对象，验证推断逻辑
        o = {"name": "X", "source_tables": [{"table": "dwd_mkt_coupon_di"}]}
        inferred = self.onto._infer_domain(o)
        self.assertEqual(inferred, "mkt")
        # 无表 → unknown
        self.assertEqual(self.onto._infer_domain({"name": "Y"}), "unknown")

    def test_relation_meta_carries_semantics(self):
        """关系语义索引：type/cardinality/description 供 OAG traverse。"""
        rels = {r["target"]: r for r in self.onto.relation_meta.get("Order", [])}
        self.assertEqual(rels["Product"]["type"], "contains")
        self.assertEqual(rels["Product"]["cardinality"], "N:1")
        self.assertTrue(rels["Product"]["description"])
        # 翻译引擎用的 edges 仍保持 (target, join_key) 兼容
        self.assertIn(("Product", "product_id"), self.onto.edges.get("Order", []))

    def test_search_by_domain_filters(self):
        """按域检索：只返回该域对象/指标。"""
        hits = self.onto.search_by_domain("gmv", "ord")
        self.assertTrue(any("[指标] gmv" in h for h in hits))
        self.assertTrue(all("域: ord" in h for h in hits))
        # usr 域没有 gmv 指标
        hits_usr = self.onto.search_by_domain("gmv", "usr")
        self.assertFalse(any("[指标] gmv" in h for h in hits_usr))
        # 无 domain 参数 = 全量检索
        all_hits = self.onto.search_by_domain("gmv")
        self.assertGreater(len(all_hits), 0)

    def test_search_index_carries_governance_fields(self):
        """检索行携带治理字段（类型/域/状态/标签），供 LLM 路由。"""
        hits = self.onto.search("订单")
        obj_line = next(h for h in hits if "[对象] Order" in h)
        self.assertIn("类型: fact", obj_line)
        self.assertIn("域: ord", obj_line)
        self.assertIn("状态: active", obj_line)
        self.assertIn("标签: 核心", obj_line)

    def test_function_domain_follows_owner(self):
        """指标域跟随归属对象。"""
        self.assertEqual(self.onto.get_function("gmv")["domain"], "ord")
        self.assertEqual(self.onto.get_function("gmv")["category"], "规模")
        self.assertEqual(self.onto.get_function("gmv")["status"], "active")
        self.assertEqual(self.onto.get_function("gmv")["unit"], "元")


if __name__ == "__main__":
    unittest.main()
