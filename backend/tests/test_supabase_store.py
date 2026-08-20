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
                     ("name", "display_name", "description", "domain", "object_type",
                      "status", "data_owner", "tags", "security_level", "update_frequency",
                      "aliases", "required_filters", "properties", "source_tables",
                      "versions", "default_version")})
    fns = []
    for f in y.functions:
        fns.append({k: f.get(k) for k in
                    ("name", "display_name", "description", "formula", "owner",
                     "domain", "category", "status", "data_owner", "unit", "tags",
                     "family", "variant_label", "default_of_family",
                     "required_filters", "supported_dimensions",
                     "supported_granularities", "do_not", "version")})
    rels = [{k: r.get(k) for k in ("source", "target", "type", "join_key",
                                   "cardinality", "description")}
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
        mock_client.return_value.return_value.get.side_effect = _fake_get
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
        mock_client.return_value.return_value.get.side_effect = _fake_get
        s_onto = Ontology(store=self.store)
        y_onto = Ontology(ONTOLOGY_DIR)
        self.assertEqual(s_onto.get_function("gmv"), y_onto.get_function("gmv"))
        self.assertEqual(set(s_onto.search("gmv")), set(y_onto.search("gmv")))
        self.assertEqual(s_onto.normalize_terms("poi 的 goods"),
                         y_onto.normalize_terms("poi 的 goods"))

    def test_get_retries_on_timeout(self):
        """网络抖动重试：第一次超时第二次成功（附带发现修复）。"""
        from core.ontology_store import _UrllibGetClient
        import urllib.request
        client = _UrllibGetClient("https://x.supabase.co", "key")
        calls = {"n": 0}
        def flaky(url, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("handshake timed out")
            resp = mock.MagicMock()
            resp.read.return_value = b"[]"
            resp.__enter__.return_value = resp
            return resp
        with mock.patch.object(urllib.request, "urlopen", side_effect=flaky):
            resp = client.get("/rest/v1/ontology_config")
            self.assertEqual(calls["n"], 2)
            self.assertEqual(resp.json(), [])  # 无数据表返回空列表

    def test_get_retries_exhausted_raises(self):
        """3 次全部超时 → RuntimeError 且带重试说明。"""
        from core.ontology_store import _UrllibGetClient
        import urllib.request
        client = _UrllibGetClient("https://x.supabase.co", "key")
        with mock.patch.object(urllib.request, "urlopen",
                               side_effect=TimeoutError("boom")):
            with self.assertRaises(RuntimeError) as ctx:
                client.get("/rest/v1/ontology_config")
            self.assertIn("重试 3 次", str(ctx.exception))

    @mock.patch("core.ontology_store._supabase_client")
    def test_select_filters_deleted(self, mock_client):
        mock_client.return_value.return_value.get.side_effect = _fake_get
        self.store.load()
        # 业务表带 is_deleted=eq.false；config（无该列）不带，避免 42703
        calls = mock_client.return_value.return_value.get.call_args_list
        biz = [c for c in calls if "ontology_config" not in c.args[0]]
        cfg = [c for c in calls if "ontology_config" in c.args[0]]
        self.assertTrue(biz, "应有业务表请求")
        for c in biz:
            self.assertEqual(c.kwargs["params"]["is_deleted"], "eq.false")
        self.assertTrue(cfg, "应有 config 请求")
        self.assertNotIn("is_deleted", cfg[0].kwargs["params"])

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

    @mock.patch("core.ontology_store.urllib.request.urlopen")
    def test_upsert_object_payload(self, mock_urlopen):
        """writer 用 urllib：验证 URL 含 on_conflict、payload revision+1。"""
        resp = mock.Mock()
        resp.status = 201
        mock_urlopen.return_value.__enter__.return_value = resp
        self.w.upsert_object({"name": "NewObj", "display_name": "新对象",
                              "revision": 3}, user="alice")
        req = mock_urlopen.call_args.args[0]
        self.assertIn("on_conflict=name", req.full_url)
        self.assertEqual(req.method, "POST")
        import json
        payload = json.loads(req.data.decode())[0]
        self.assertEqual(payload["revision"], 4)          # 乐观锁 +1
        self.assertEqual(payload["updated_by"], "alice")
        self.assertIn("resolution=merge-duplicates", req.headers["Prefer"])

    @mock.patch("core.ontology_store.urllib.request.urlopen")
    def test_upsert_table_payload(self, mock_urlopen):
        """upsert_table 走 on_conflict=table_name。"""
        resp = mock.Mock()
        resp.status = 201
        mock_urlopen.return_value.__enter__.return_value = resp
        self.w.upsert_table({"table_name": "dwd_x", "layer": "DWD"})
        req = mock_urlopen.call_args.args[0]
        self.assertIn("on_conflict=table_name", req.full_url)

    @mock.patch("core.ontology_store.urllib.request.urlopen")
    def test_upsert_config_no_revision_column(self, mock_urlopen):
        """config 是纯键值表（无 revision/updated_by 列）→ payload 不带这两列。

        回归：早前 _upsert_row 无条件写 revision/updated_by 会导致
        PostgREST 42703 undefined column。
        """
        resp = mock.Mock()
        resp.status = 201
        mock_urlopen.return_value.__enter__.return_value = resp
        self.w.upsert_config("time_dimension", "dt", user="alice")
        req = mock_urlopen.call_args.args[0]
        self.assertIn("on_conflict=key", req.full_url)
        import json
        payload = json.loads(req.data.decode())[0]
        self.assertEqual(payload["key"], "time_dimension")
        self.assertEqual(payload["value"], '"dt"')        # JSON 序列化
        self.assertNotIn("revision", payload)             # 无该列
        self.assertNotIn("updated_by", payload)           # 无该列

    @mock.patch("core.ontology_store.urllib.request.urlopen")
    def test_upsert_many_batch(self, mock_urlopen):
        """upsert_many 逐行 upsert，返回行数（供导入脚本复用）。"""
        resp = mock.Mock()
        resp.status = 201
        mock_urlopen.return_value.__enter__.return_value = resp
        n = self.w.upsert_many("ontology_objects", [
            {"name": "A"}, {"name": "B"}], user="import")
        self.assertEqual(n, 2)
        self.assertEqual(mock_urlopen.call_count, 2)

    @mock.patch("core.ontology_store.urllib.request.urlopen")
    def test_soft_delete_payload(self, mock_urlopen):
        """软删除：PATCH is_deleted=true，按主键过滤，带 updated_by。"""
        resp = mock.Mock()
        resp.status = 204
        mock_urlopen.return_value.__enter__.return_value = resp
        self.w.soft_delete("ontology_objects", "name", "Survey", user="cleanup")
        req = mock_urlopen.call_args.args[0]
        self.assertEqual(req.method, "PATCH")
        self.assertIn("name=eq.Survey", req.full_url)
        self.assertIn("is_deleted=eq.false", req.full_url)
        import json
        payload = json.loads(req.data.decode())
        self.assertTrue(payload["is_deleted"])
        self.assertEqual(payload["updated_by"], "cleanup")

    @mock.patch("core.ontology_store.urllib.request.urlopen")
    def test_soft_delete_already_gone_idempotent(self, mock_urlopen):
        """软删除已不存在的行（HTTP 404）→ 幂等成功不报错。"""
        from urllib.error import HTTPError
        mock_urlopen.side_effect = HTTPError("", 404, "not found", None, None)
        self.w.soft_delete("ontology_objects", "name", "Ghost")  # 不抛异常

    @mock.patch("core.ontology_store.urllib.request.urlopen")
    def test_update_object_revision_conflict(self, mock_urlopen):
        """PATCH 命中 0 行（204）→ 冲突。"""
        from urllib.error import HTTPError
        mock_urlopen.side_effect = HTTPError("", 204, "no content", None, None)
        ok = self.w.update_object_if_revision("Order", {"name": "Order"},
                                              expected_revision=5, user="alice")
        self.assertFalse(ok)  # 他人已改，revision 5 不存在 → 冲突

    @mock.patch("core.ontology_store.urllib.request.urlopen")
    def test_update_object_revision_ok(self, mock_urlopen):
        """PATCH 命中（200 带 body）→ 成功；请求带 revision 条件。"""
        resp = mock.Mock()
        resp.status = 200
        resp.read.return_value = b'[{"id": 1}]'
        mock_urlopen.return_value.__enter__.return_value = resp
        ok = self.w.update_object_if_revision("Order", {"name": "Order"},
                                              expected_revision=5, user="alice")
        self.assertTrue(ok)
        req = mock_urlopen.call_args.args[0]
        self.assertIn("revision=eq.5", req.full_url)
        self.assertEqual(req.method, "PATCH")


class NormalizeRowsTest(unittest.TestCase):
    """normalize_rows：键对齐 + NOT NULL 类型安全（防 PGRST102/23502/22P02）。"""

    def test_aligns_keys(self):
        from core.ontology_store import normalize_rows, SUPABASE_ROW_KEYS
        rows = [{"name": "A", "display_name": "甲"},
                {"name": "B"}]  # B 缺 display_name
        out = normalize_rows(rows, SUPABASE_ROW_KEYS["ontology_objects"])
        self.assertEqual(set(out[0].keys()), set(out[1].keys()))
        self.assertEqual(out[1]["display_name"], "")

    def test_jsonb_none_becomes_array(self):
        from core.ontology_store import normalize_rows, SUPABASE_ROW_KEYS
        rows = [{"name": "A"}]  # 缺 aliases
        out = normalize_rows(rows, SUPABASE_ROW_KEYS["ontology_objects"])
        self.assertEqual(out[0]["aliases"], "[]")
        self.assertEqual(out[0]["properties"], "[]")

    def test_bool_none_becomes_false(self):
        from core.ontology_store import normalize_rows, SUPABASE_ROW_KEYS
        rows = [{"name": "gmv", "formula": "SUM(x)", "owner": "Payment"}]  # 缺 default_of_family
        out = normalize_rows(rows, SUPABASE_ROW_KEYS["ontology_functions"])
        self.assertIs(out[0]["default_of_family"], False)

    def test_yaml_rows_normalize_cleanly(self):
        """真实 YAML 数据经过 normalize 后键一致且类型安全。"""
        from core.ontology_store import (
            SUPABASE_ROW_KEYS, YamlOntologyStore, normalize_rows)
        y = YamlOntologyStore(ONTOLOGY_DIR).load()
        objs = normalize_rows(y.objects, SUPABASE_ROW_KEYS["ontology_objects"])
        fns = normalize_rows(y.functions, SUPABASE_ROW_KEYS["ontology_functions"])
        # 所有行键一致
        self.assertEqual(len({frozenset(r.keys()) for r in objs}), 1)
        self.assertEqual(len({frozenset(r.keys()) for r in fns}), 1)
        # 无 None（避免 NOT NULL 违反）
        self.assertTrue(all(v is not None for r in objs for v in r.values()))
        self.assertTrue(all(v is not None for r in fns for v in r.values()))


if __name__ == "__main__":
    unittest.main()
