"""P1-6 方言翻译快照回归：同一批 MQL 在五种方言下翻译的 SQL 文本必须稳定。

作用：
  * 锁定方言映射（时间表达式/粒度分组），防止改动 translator 时悄悄破坏
    mysql/doris/hive/sparksql 的输出
  * 与 eval（只跑 sqlite）互补：eval 验证「翻译正确性」，快照验证「方言稳定性」

运行：backend/.venv/bin/python -m unittest tests.test_dialect_snapshots
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from core import Ontology, Translator  # noqa: E402

DIALECTS = ["sqlite", "mysql", "doris", "hive", "sparksql"]

# 固定用例：key 为描述，value 为 MQL
CASES: dict[str, dict] = {
    "yesterday_gmv": {"metrics": ["gmv"]},
    "week_gmv": {"metrics": ["gmv"],
                 "dimensions": [{"name": "order_date", "granularity": "week"}]},
    "month_gmv_30d": {"metrics": ["gmv"],
                      "time_range": {"start": "-30d"},
                      "dimensions": [{"name": "order_date", "granularity": "month"}]},
    "gmv_by_store": {"metrics": ["gmv"],
                     "dimensions": [{"name": "store_type"}],
                     "time_range": {"start": "20260701", "end": "20260731"}},
    "multi_metric_cross_domain": {"metrics": ["gmv", "refund_amount"],
                                  "time_range": {"start": "20260701", "end": "20260731"}},
    "filter_city": {"metrics": ["gmv"],
                    "filters": [{"field": "city", "operator": "eq", "value": "上海"}],
                    "time_range": "20260705"},
    "top5_by_product": {"metrics": ["order_count"],
                        "dimensions": [{"name": "product_type"}],
                        "sort": [{"field": "order_count", "order": "desc"}],
                        "limit": 5,
                        "time_range": {"start": "-90d"}},
}

# 各方言关键特征断言：出现/不出现（用最小必要断言，避免脆性全文匹配）
DIALECT_FEATURES: dict[str, tuple[list[str], list[str]]] = {
    "sqlite": (["strftime("], []),
    "mysql": (["DATE_FORMAT(", "DATE_SUB(CURDATE()"], []),
    "doris": (["DATE_FORMAT(", "DATE_SUB(CURDATE()"], ["strftime("]),
    "hive": (["FROM_UNIXTIME("], ["strftime("]),
    "sparksql": (["DATE_FORMAT(", "DATE_SUB(CURRENT_DATE"], ["strftime("]),
}

# 仅在特定用例必须出现的特征（week 用例才出现周函数）
CASE_FEATURES: dict[str, tuple[list[str], list[str]]] = {
    "sqlite": (["iso_week("], []),
    "mysql": (["%x-W%v"], []),
    "doris": (["%x-W%v"], []),
    "hive": (["WEEKOFYEAR("], []),
    "sparksql": (["WEEKOFYEAR("], []),
}


class DialectSnapshotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.onto = Ontology(BACKEND / "ontology")
        cls.translator = Translator(cls.onto)

    def test_all_dialects_translate(self):
        """每个用例在每种方言下都能成功翻译（无 error）。"""
        for d in DIALECTS:
            for name, mql in CASES.items():
                r = self.translator.translate(mql, {}, dialect=d)
                self.assertNotIn("error", r, f"[{d}] {name} 翻译失败: {r.get('error')}")

    def test_dialect_feature_assertions(self):
        """方言特征断言：每种方言的 SQL 必须包含/不包含关键函数。"""
        # 显式日期区间（start/end/单日字符串）的用例不含相对时间函数，豁免时间特征
        explicit_time = {"gmv_by_store", "multi_metric_cross_domain", "filter_city"}
        for d in DIALECTS:
            must, must_not = DIALECT_FEATURES[d]
            case_must, case_must_not = CASE_FEATURES[d]
            for name, mql in CASES.items():
                r = self.translator.translate(mql, {}, dialect=d)
                self.assertNotIn("error", r, f"[{d}] {name} 翻译失败")
                sql = r["sql"]
                for frag in must:
                    if name in explicit_time:
                        continue  # 绝对时间用例不要求时间函数
                    self.assertIn(frag, sql, f"[{d}] {name} 缺少特征 {frag!r}: {sql}")
                for frag in must_not:
                    self.assertNotIn(frag, sql, f"[{d}] {name} 不应出现 {frag!r}: {sql}")
                if name == "week_gmv":
                    for frag in case_must:
                        self.assertIn(frag, sql, f"[{d}] week 用例缺少特征 {frag!r}: {sql}")
                    for frag in case_must_not:
                        self.assertNotIn(frag, sql, f"[{d}] week 用例不应出现 {frag!r}: {sql}")

    def test_sqlite_snapshot_full_text(self):
        """sqlite 输出全文快照：稳定锁定，防止回归。"""
        expected = {
            "yesterday_gmv": "SELECT SUM(F.gmv_amt) AS gmv FROM ads_ord_gmv_1d F "
                             "WHERE F.dt = strftime('%Y%m%d', date('now','-1 day'))",
            "filter_city": "SELECT (SUM(F.pay_amt)) AS gmv FROM dwd_ord_pay_di F "
                           "JOIN dim_store S ON S.store_id = F.store_id "
                           "WHERE F.is_valid = 1 AND S.city = '上海' AND F.dt = '20260705'",
        }
        for name, want in expected.items():
            r = self.translator.translate(CASES[name], {}, dialect="sqlite")
            self.assertEqual(r["sql"], want)

    def test_unsupported_dialect_rejected(self):
        r = self.translator.translate(CASES["yesterday_gmv"], {}, dialect="clickhouse")
        self.assertIn("error", r)
        self.assertIn("不支持的目标方言", r["error"])


if __name__ == "__main__":
    unittest.main()
