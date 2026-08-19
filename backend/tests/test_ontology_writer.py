"""本体写入器测试（多人编辑闭环）：YAML 通道 / Supabase 通道 / 分发 / 拒绝。

运行：backend/.venv/bin/python -m unittest tests.test_ontology_writer
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import yaml  # noqa: E402

from core.ontology_writer import (  # noqa: E402
    YamlOntologyWriter, create_writer, writer_supports)

ONTOLOGY_DIR = BACKEND / "ontology"


class YamlWriterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="da-writer-"))
        # 复制最小 glossary 作为基底
        src = ONTOLOGY_DIR / "glossary.yaml"
        (self.tmp / "glossary.yaml").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        self.w = YamlOntologyWriter(self.tmp)

    def test_upsert_object_new(self):
        self.w.upsert_object({"name": "NewObj", "display_name": "新对象"})
        raw = yaml.safe_load((self.tmp / "objects.yaml").read_text(encoding="utf-8"))
        names = [o["name"] for o in raw.get("objects", [])]
        self.assertIn("NewObj", names)

    def test_upsert_glossary_idempotent(self):
        """同一 term 重复注册不重复追加（upsert 语义）。"""
        for _ in range(2):
            self.w.upsert_glossary({"term": "__dup__", "canonical": "X", "type": "object"})
        raw = yaml.safe_load((self.tmp / "glossary.yaml").read_text(encoding="utf-8"))
        cnt = sum(1 for g in raw.get("glossary", []) if g["term"] == "__dup__")
        self.assertEqual(cnt, 1)

    def test_upsert_function_replaces(self):
        self.w.upsert_function({"name": "gmv", "formula": "SUM(pay_amount)",
                                "owner": "Payment", "version": "v9.9"})
        raw = yaml.safe_load((self.tmp / "functions.yaml").read_text(encoding="utf-8"))
        gmv = [f for f in raw.get("functions", []) if f["name"] == "gmv"]
        self.assertEqual(gmv[0]["version"], "v9.9")

    def test_upsert_config(self):
        self.w.upsert_config("time_dimension", "order_date")
        cfg = yaml.safe_load((self.tmp / "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(cfg["time_dimension"], "order_date")


class WriterFactoryTest(unittest.TestCase):
    def test_create_yaml(self):
        w = create_writer("yaml", base="/tmp/x")
        self.assertIsInstance(w, YamlOntologyWriter)

    def test_create_supabase(self):
        w = create_writer("supabase", url="https://x", key="k")
        self.assertEqual(type(w).__name__, "SupabaseOntologyWriter")

    def test_create_supabase_requires_creds(self):
        with self.assertRaises(ValueError):
            create_writer("supabase")

    def test_sqlite_returns_readonly(self):
        """sqlite 不再抛异常（server 能启动），返回只读占位。"""
        from core.ontology_writer import ReadOnlyWriter
        w = create_writer("sqlite", base="/tmp")
        self.assertIsInstance(w, ReadOnlyWriter)

    def test_unsupported(self):
        with self.assertRaises(ValueError):
            create_writer("postgres")

    def test_writer_supports(self):
        self.assertTrue(writer_supports("yaml"))
        self.assertTrue(writer_supports("supabase"))
        self.assertFalse(writer_supports("sqlite"))


class SupabaseWriterChannelTest(unittest.TestCase):
    """Supabase 通道：确认 writer 走 SupabaseOntologyWriter 的 upsert。"""

    @mock.patch("core.ontology_store.SupabaseOntologyWriter.upsert_object")
    def test_register_object_goes_to_supabase(self, mock_upsert):
        from core.ontology_writer import create_writer
        w = create_writer("supabase", url="https://x", key="k")
        w.upsert_object({"name": "Order"})
        mock_upsert.assert_called_once_with({"name": "Order"})


if __name__ == "__main__":
    unittest.main()


class ReadOnlyWriterTest(unittest.TestCase):
    """sqlite 模式：只读占位，写操作报错，server 能启动。"""

    def test_sqlite_writer_is_readonly(self):
        from core.ontology_writer import create_writer, ReadOnlyWriter
        w = create_writer("sqlite")
        self.assertIsInstance(w, ReadOnlyWriter)
        with self.assertRaises(ValueError) as ctx:
            w.upsert_object({"name": "X"})
        self.assertIn("只读", str(ctx.exception))

    def test_writer_supports_false_for_sqlite(self):
        from core.ontology_writer import writer_supports
        self.assertFalse(writer_supports("sqlite"))
        self.assertTrue(writer_supports("yaml"))
        self.assertTrue(writer_supports("supabase"))
