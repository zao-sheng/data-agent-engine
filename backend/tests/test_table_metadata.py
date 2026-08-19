"""表元数据测试：采集器完整性 / TableMetadataStore 查库 / 回落逻辑。

运行：backend/.venv/bin/python -m unittest tests.test_table_metadata
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from core import Ontology  # noqa: E402
from core.metadata import MetadataService  # noqa: E402
from core.ontology_store import TableMetadataStore  # noqa: E402
from core.table_metadata import collect_table_metadata  # noqa: E402

ONTOLOGY = Ontology(BACKEND / "ontology")
DB = BACKEND / "seed" / "sample.db"


class CollectorTest(unittest.TestCase):
    def test_collects_all_15_tables(self):
        metas = collect_table_metadata(DB, ONTOLOGY)
        self.assertEqual(len(metas), 15)
        names = {m["table_name"] for m in metas}
        self.assertIn("dwd_ord_pay_di", names)
        self.assertIn("dim_store", names)
        self.assertIn("ads_ord_gmv_1d", names)

    def test_field_structure(self):
        metas = {m["table_name"]: m for m in collect_table_metadata(DB, ONTOLOGY)}
        pay = metas["dwd_ord_pay_di"]
        self.assertEqual(pay["layer"], "DWD")
        self.assertEqual(pay["partition_col"], "dt")
        self.assertTrue(any(f["is_pk"] for f in pay["fields"]))
        # 分区列标记
        self.assertTrue(any(f["is_partition"] and f["name"] == "dt" for f in pay["fields"]))
        # 血缘
        self.assertEqual(pay["lineage"]["source"], "")  # DWD 贴源无上游

    def test_lineage_for_agg_tables(self):
        metas = {m["table_name"]: m for m in collect_table_metadata(DB, ONTOLOGY)}
        gmv = metas["ads_ord_gmv_1d"]
        self.assertEqual(gmv["lineage"]["source"], "dwd_ord_pay_di")
        self.assertIn("GMV", gmv["lineage"]["logic"])
        self.assertEqual(gmv["readiness"], "T+1")

    def test_dim_tables_no_partition(self):
        metas = {m["table_name"]: m for m in collect_table_metadata(DB, ONTOLOGY)}
        store = metas["dim_store"]
        self.assertEqual(store["layer"], "DIM")
        self.assertEqual(store["partition_col"], "")
        self.assertEqual(store["granularity"], "维度")


class TableMetadataStoreTest(unittest.TestCase):
    """TableMetadataStore：mock PostgREST 返回，验证行→结构转换。"""

    def _fake_client(self, row: dict | None):
        """构造返回固定行的 fake client 工厂（匹配 _supabase_client()(url,key,schema).get）。"""
        class FakeResp:
            def raise_for_status(self):
                pass
            def json(self):
                return [row] if row else []

        class FakeGet:
            def __init__(self, *a, **k):
                pass
            def get(self, path, params=None, headers=None):
                return FakeResp()

        import core.ontology_store as os_mod
        return mock.patch.object(os_mod, "_supabase_client", return_value=FakeGet)

    def test_table_info_parses_row(self):
        row = {
            "table_name": "dwd_ord_pay_di", "layer": "DWD",
            "description": "支付明细", "granularity": "明细_原子",
            "partition_col": "dt", "owner_object": "Payment",
            "fields": [{"name": "pay_id", "type": "TEXT", "is_pk": True}],
            "lineage": {"source": "", "logic": "贴源"},
            "readiness": "T+1", "row_estimate": 1000,
        }
        with self._fake_client(row):
            store = TableMetadataStore("https://x", "k")
            info = store.table_info("dwd_ord_pay_di")
        self.assertEqual(info["table"], "dwd_ord_pay_di")
        self.assertEqual(info["layer_cn"], "明细层")
        self.assertEqual(info["owner_object"], "Payment")
        self.assertEqual(info["fields"][0]["name"], "pay_id")
        self.assertEqual(info["lineage"]["source"], "")

    def test_table_not_found_returns_none(self):
        with self._fake_client(None):
            store = TableMetadataStore("https://x", "k")
            self.assertIsNone(store.table_info("nope"))


class MetadataFallbackTest(unittest.TestCase):
    """metadata_search 查库优先 / 回落本地。"""

    def test_store_priority_when_table_present(self):
        """表在 ontology_tables 时用库数据（含血缘/就绪）。"""
        store_row = {
            "table_name": "ads_ord_gmv_1d", "layer": "ADS",
            "description": "GMV 应用层", "granularity": "日",
            "partition_col": "dt", "owner_object": "Payment",
            "fields": [{"name": "gmv_amt", "type": "REAL", "is_pk": False}],
            "lineage": {"source": "dwd_ord_pay_di", "logic": "按 dt 聚合 GMV"},
            "readiness": "T+2", "row_estimate": 90,
        }
        import core.ontology_store as os_mod
        class FakeResp:
            def raise_for_status(self): pass
            def json(self): return [store_row]
        class FakeGet:
            def __init__(self, *a, **k): pass
            def get(self, path, params=None, headers=None): return FakeResp()
        with mock.patch.object(os_mod, "_supabase_client", return_value=FakeGet):
            tstore = TableMetadataStore("https://x", "k")
            svc = MetadataService(ONTOLOGY, db_path=str(DB), tables_store=tstore)
            info = svc.table_info("ads_ord_gmv_1d")
            lin = svc.lineage("ads_ord_gmv_1d")
            ready = svc.readiness("ads_ord_gmv_1d")
        self.assertEqual(info["owner_object"], "Payment")
        self.assertEqual(lin["source"], "dwd_ord_pay_di")
        self.assertEqual(ready["ready_partition"], "T+2")  # 库优先于本地 t-1

    def test_fallback_local_when_no_store(self):
        svc = MetadataService(ONTOLOGY, db_path=str(DB), tables_store=None)
        info = svc.table_info("ads_ord_gmv_1d")
        self.assertEqual(info["layer"], "ADS")
        lin = svc.lineage("ads_ord_gmv_1d")
        self.assertEqual(lin["source"], "dwd_ord_pay_di")
        ready = svc.readiness("ads_ord_gmv_1d")
        self.assertIn("t-1", ready["ready_partition"])


if __name__ == "__main__":
    unittest.main()
