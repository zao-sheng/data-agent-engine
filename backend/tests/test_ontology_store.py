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


if __name__ == "__main__":
    unittest.main()
