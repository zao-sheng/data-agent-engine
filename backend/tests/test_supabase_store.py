"""Supabase 本体存储测试（阶段 2）：读一致性 / 行→YAML 形状 / 写 payload / 乐观锁。

用 unittest.mock 模拟 PostgREST HTTP，不依赖真实 Supabase 连接。
运行：backend/.venv/bin/python -m unittest tests.test_supabase_store
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from core import Ontology  # noqa: E402
from core.ontology_store import (  # noqa: E402
    SupabaseOntologyStore, SupabaseOntologyWriter, YamlOntologyStore,
    compile_to_sqlite, create_store)

ONTOLOGY_DIR = BACKEND / "ontology"


def _sample_supabase_rows() -> dict[str, list[dict]]:
    """从 YAML 构造 Supabase 行（模拟 DB 里已建表且有数据）。"""
    y = YamlOntologyStore(ONTOLOGY_DIR).load()
    objs = []
    for o in y.objects:
        objs.append({k: o.get(k) for k in
                     ("name", "display_name", "description", "aliases",
                      "required_filters", "properties", "source_tables",
                      "versions", "default_version")})
    fns = []
    for f in y.functions:
        fns.append({k: f.get(k) for k in
                    ("name", "display_name", "description", "formula", "owner",
                     "family", "variant_label", "default_of_family",
                     "required_filters", "supported_dimensions",
                     "supported_granularities", "do_not", "version")})
    rels = [{k: r.get(k) for k in ("source", "target", "type", "join_key", "cardinality")}
            for r in y.relations]
    gloss = [{k: g.get(k) for k in ("term", "canonical", "type")} for g in y.glossary]
    cfg = [{"key": k, "value": __import__("json").dumps(v, ensure_ascii=False)}
           for k, v in y.config.items()]
    return {"ontology_objects": objs, "ontology_functions": fns,
            "ontology_relations": rels, "ontology_glossary": gloss,
            "ontology_config": cfg}


def _fake_get(url, params=None, headers=None):
    table = url.rsplit("/", 1)[-1]
    rows = _sample_supabase_rows().get(table, [])
    resp = mock.Mock()
    resp.raise_for_status = lambda: None
    resp.json.return_value = rows
    return resp


class SupabaseStoreReadTest(unittest.TestCase):
    def setUp(self):
        self.store = SupabaseOntologyStore("https://x.supabase.co", "key")

    @mock.patch("core.ontology_store._supabase_client")
    def test_load_matches_yaml_data(self, mock_client):
        mock_client.return_value.get.side_effect = _fake_get
        data = self.store.load()

        y = YamlOntologyStore(ONTOLOGY_DIR).load()
        # 数量与内容一致
        self.assertEqual({o["name"] for o in data.objects},
                         {o["name"] for o in y.objects})
        self.assertEqual({f["name"] for f in data.functions},
                         {f["name"] for f in y.functions})
        self.assertEqual(data.relations, y.relations)
        self.assertEqual(data.glossary, y.glossary)
        self.assertEqual(data.config, y.config)

    @mock.patch("core.ontology_store._supabase_client")
    def test_ontology_built_from_supabase_behaves_like_yaml(self, mock_client):
        mock_client.return_value.get.side_effect = _fake_get
        s_onto = Ontology(store=self.store)
        y_onto = Ontology(ONTOLOGY_DIR)
        self.assertEqual(s_onto.get_function("gmv"), y_onto.get_function("gmv"))
        self.assertEqual(set(s_onto.search("gmv")), set(y_onto.search("gmv")))
        self.assertEqual(s_onto.normalize_terms("poi 的 goods"),
                         y_onto.normalize_terms("poi 的 goods"))

    @mock.patch("core.ontology_store._supabase_client")
    def test_select_filters_deleted(self, mock_client):
        mock_client.return_value.get.side_effect = _fake_get
        self.store.load()
        # 断言请求带了 is_deleted=eq.false
        calls = mock_client.return_value.get.call_args_list
        for c in calls:
            self.assertEqual(c.kwargs["params"]["is_deleted"], "eq.false")

    def test_create_store_supabase_requires_creds(self):
        with self.assertRaises(ValueError):
            create_store("supabase")
        with self.assertRaises(ValueError):
            create_store("supabase", url="https://x", key=None)
        s = create_store("supabase", url="https://x", key="k")
        self.assertIsInstance(s, SupabaseOntologyStore)


class SupabaseWriterTest(unittest.TestCase):
    def setUp(self):
        self.w = SupabaseOntologyWriter("https://x.supabase.co", "key")

    @mock.patch("core.ontology_store._supabase_client")
    def test_upsert_object_payload(self, mock_client):
        resp = mock.Mock()
        resp.raise_for_status = lambda: None
        mock_client.return_value.post.return_value = resp
        self.w.upsert_object({"name": "NewObj", "display_name": "新对象",
                              "revision": 3}, user="alice")
        args = mock_client.return_value.post.call_args
        url = args.args[0]
        payload = args.kwargs["json"][0]
        headers = args.kwargs["headers"]
        self.assertTrue(url.endswith("/rest/v1/ontology_objects"))
        self.assertEqual(payload["revision"], 4)          # 乐观锁 +1
        self.assertEqual(payload["updated_by"], "alice")
        self.assertIn("resolution=merge-duplicates", headers["Prefer"])
        self.assertIn("Content-Profile", headers)

    @mock.patch("core.ontology_store._supabase_client")
    def test_update_object_revision_conflict(self, mock_client):
        # 模拟 PATCH 命中 0 行（204 无 body）→ 冲突
        resp = mock.Mock()
        resp.raise_for_status = lambda: None
        resp.status_code = 204
        mock_client.return_value.patch.return_value = resp
        ok = self.w.update_object_if_revision("Order", {"name": "Order"},
                                              expected_revision=5, user="alice")
        self.assertFalse(ok)  # 他人已改，revision 5 不存在 → 冲突

    @mock.patch("core.ontology_store._supabase_client")
    def test_update_object_revision_ok(self, mock_client):
        resp = mock.Mock()
        resp.raise_for_status = lambda: None
        resp.status_code = 200
        resp.json.return_value = [{"id": 1}]
        mock_client.return_value.patch.return_value = resp
        ok = self.w.update_object_if_revision("Order", {"name": "Order"},
                                              expected_revision=5, user="alice")
        self.assertTrue(ok)
        # 请求带 revision 条件
        params = mock_client.return_value.patch.call_args.kwargs["params"]
        self.assertEqual(params["revision"], "eq.5")


if __name__ == "__main__":
    unittest.main()
