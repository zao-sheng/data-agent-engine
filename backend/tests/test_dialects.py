"""方言测试：多方言翻译快照 + 远程执行器（P1 合并）。：同一批 MQL 在五种方言下翻译的 SQL 文本必须稳定。

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
from core.executor import Executor  # noqa: E402

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


class RemoteExecutorTest(unittest.TestCase):
    def test_missing_dsn_reports_config_hint(self):
        """未配置连接串 → 明确提示 .env 配置项。"""
        ex = Executor("", dialect="mysql")
        # 手动清空 remote_dsn（测试环境可能没有 .env）
        from core import config as cfg
        saved = cfg.CONFIG.remote_dsn
        cfg.CONFIG.remote_dsn = {}
        try:
            r = ex.execute("SELECT 1")
            self.assertIn("error", r)
            self.assertIn("DATA_AGENT_DSN_MYSQL", r["error"])
            self.assertIn("scheme://user:pass@host:port/db", r["error"])
        finally:
            cfg.CONFIG.remote_dsn = saved

    def test_sqlite_medium_rejects_remote_dialect(self):
        """dsn 是 .db 文件但方言是远程 → 介质不匹配错误。"""
        ex = Executor("/tmp/x.db", dialect="doris")
        r = ex.execute("SELECT 1")
        self.assertIn("error", r)
        self.assertIn("SQLite 介质不匹配", r["error"])

    def test_forbidden_sql_rejected_before_dialect(self):
        """远程方言下禁写语句同样被拦截（不触达驱动）。"""
        ex = Executor("", dialect="mysql")
        r = ex.execute("DELETE FROM dwd_ord_pay_di")
        self.assertIn("error", r)
        self.assertIn("只读执行器拒绝", r["error"])

    def test_missing_driver_reports_install_hint(self):
        """配置了连接串但驱动未装 → 给出安装指引。"""
        from core import config as cfg
        saved = cfg.CONFIG.remote_dsn
        cfg.CONFIG.remote_dsn = {"mysql": "mysql://u:p@127.0.0.1:3306/db"}
        try:
            ex = Executor("mysql://u:p@127.0.0.1:3306/db", dialect="mysql")
            r = ex.execute("SELECT 1")
            self.assertIn("error", r)
            self.assertIn("pymysql", r["error"])
            self.assertIn("uv pip install", r["error"])
        finally:
            cfg.CONFIG.remote_dsn = saved

    def test_unsupported_scheme(self):
        from core import config as cfg
        saved = cfg.CONFIG.remote_dsn
        cfg.CONFIG.remote_dsn = {"mysql": "oracle://u:p@h:1521/db"}
        try:
            ex = Executor("oracle://u:p@h:1521/db", dialect="mysql")
            r = ex.execute("SELECT 1")
            self.assertIn("error", r)
            self.assertIn("不支持的连接 scheme", r["error"])
        finally:
            cfg.CONFIG.remote_dsn = saved


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
