"""元数据检索（元数据咨询意图）：基于 Ontology + 样例库 schema 的实现。

覆盖企业元数据查询的常见诉求：
  1. 找表：表名 / 层（DWD/DWS/ADS/DIM）/ 字段 / 粒度 / 预聚合指标
  2. 找指标维度：指标口径（公式/过滤/版本/归属对象）/ 属性归属
  3. 表加工逻辑（血缘）：DWD 明细 → DWS/ADS 汇总的加工链路
  4. 就绪时间：样例库 t-1 就绪（真实接入后由元数据接口提供）

设计：本模块是**本地实现**（从 Ontology 与样例 schema 推导）；
后续用户接入真实元数据平台后，替换 `MetadataService.search` 的
数据源即可，对外返回结构保持不变。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .ontology_loader import Ontology

# 层 → 中文名
LAYER_CN = {"ODS": "贴源层", "DWD": "明细层", "DWS": "汇总层",
            "ADS": "应用层", "DIM": "维度层"}

# 表加工逻辑（血缘）示例：真实场景来自元数据平台 lineage，这里从样例 ETL 语义推导
# key = 汇总/应用表名，value = 来源表 + 加工说明
_LINEAGE: dict[str, dict] = {
    "dws_ord_order_1d": {"source": "dwd_ord_order_di",
                         "logic": "按 dt/region_id/store_id 聚合：订单量、有效订单量、下单金额、下单用户数"},
    "dws_ord_pay_1d": {"source": "dwd_ord_pay_di",
                       "logic": "按 dt/region_id/store_id 聚合：支付笔数、支付金额、支付用户数（过滤 is_valid=1）"},
    "dws_ord_consume_1d": {"source": "dwd_ord_consume_di",
                           "logic": "按 dt/region_id/store_id 聚合：消费笔数、消费金额、消费用户数（过滤 is_valid=1）"},
    "dws_ord_refund_1d": {"source": "dwd_ord_refund_di",
                          "logic": "按 dt/region_id/store_id 聚合：退款笔数、退款金额（过滤 is_valid=1）"},
    "ads_ord_gmv_1d": {"source": "dwd_ord_pay_di",
                       "logic": "按 dt 聚合：GMV（SUM(pay_amt)，过滤 is_valid=1）"},
    "ads_ord_gmv_1m": {"source": "dwd_ord_pay_di",
                       "logic": "按月聚合：GMV、支付笔数（过滤 is_valid=1）"},
    "ads_ord_user_1d": {"source": "dwd_ord_pay_di",
                        "logic": "按 dt 聚合：活跃用户数（样例置 0）、下单用户数、新用户数（样例置 0）"},
}


class MetadataService:
    def __init__(self, onto: Ontology, db_path: str | Path | None = None):
        self.onto = onto
        self.db_path = Path(db_path) if db_path else None

    # ── 表检索 ──────────────────────────────────────────────
    def table_info(self, table: str) -> dict | None:
        """按物理表名查表信息（层/字段/粒度/预聚合/来源对象）。"""
        table = table.lower()
        for obj_name, o in self.onto.objects.items():
            for t in o.get("source_tables", []):
                if t["table"] == table:
                    props = []
                    for p in o.get("properties", []):
                        col = t.get("field_mapping", {}).get(p["name"])
                        props.append({"name": p["name"], "column": col,
                                      "type": p.get("type"), "description": p.get("description", "")})
                    return {
                        "table": table,
                        "layer": t.get("layer"),
                        "layer_cn": LAYER_CN.get(t.get("layer"), t.get("layer")),
                        "business_object": obj_name,
                        "object_display": o.get("display_name"),
                        "granularities": t.get("granularities", []),
                        "joinable": t.get("joinable", False),
                        "authority": t.get("authority"),
                        "perm_column": t.get("perm_column"),
                        "fields": props,
                        "pre_aggregated": list((t.get("pre_aggregated") or {}).keys()),
                        "description": o.get("description", ""),
                    }
        return None

    def find_tables(self, keyword: str) -> list[dict]:
        """按关键字找表（表名 / 对象名 / 展示名 / 描述模糊匹配）。"""
        kw = keyword.strip().lower()
        hits = []
        for obj_name, o in self.onto.objects.items():
            for t in o.get("source_tables", []):
                hay = " ".join([t["table"], obj_name, o.get("display_name", ""),
                                o.get("description", ""), t.get("layer", "")]).lower()
                if kw and kw in hay:
                    info = self.table_info(t["table"])
                    if info:
                        hits.append(info)
        return hits

    # ── 指标检索 ────────────────────────────────────────────
    def metric_info(self, name: str) -> dict | None:
        """按指标名查口径（公式/过滤/版本/归属/族）。"""
        fn = self.onto.get_function(name)
        if not fn:
            return None
        return {
            "name": fn["name"],
            "display_name": fn.get("display_name"),
            "description": fn.get("description", ""),
            "formula": fn.get("formula"),
            "owner": fn.get("owner"),
            "version": fn.get("version"),
            "family": fn.get("family"),
            "variant_label": fn.get("variant_label"),
            "default_of_family": fn.get("default_of_family", False),
            "required_filters": fn.get("required_filters", []),
            "supported_dimensions": fn.get("supported_dimensions", []),
            "do_not": fn.get("do_not", ""),
        }

    def find_metrics(self, keyword: str) -> list[dict]:
        """按关键字找指标（名称/展示名/描述/族/口径词）。"""
        kw = keyword.strip().lower()
        hits = []
        for fn in self.onto.functions.values():
            hay = " ".join([fn["name"], fn.get("display_name", ""),
                            fn.get("description", ""), fn.get("family", ""),
                            fn.get("variant_label", "")]).lower()
            if kw and kw in hay:
                info = self.metric_info(fn["name"])
                if info:
                    hits.append(info)
        return hits

    # ── 血缘 / 加工逻辑 ──────────────────────────────────────
    def lineage(self, table: str) -> dict | None:
        """查表的加工逻辑（血缘）：来源表 + 加工说明；样例库内置，真实场景接元数据平台。"""
        table = table.lower()
        if table in _LINEAGE:
            return {"table": table, **_LINEAGE[table]}
        # DWD 明细无上游加工（贴源），返回空
        for obj_name, o in self.onto.objects.items():
            for t in o.get("source_tables", []):
                if t["table"] == table and t.get("layer") == "DWD":
                    return {"table": table, "source": "ODS/上游系统（样例库由 seed 直接生成）",
                            "logic": "明细层：清洗后原子事实，无聚合加工"}
        return None

    # ── 就绪时间 ─────────────────────────────────────────────
    def readiness(self, table: str) -> dict:
        """表就绪时间（样例库约定 t-1；真实场景由元数据接口返回）。"""
        return {"table": table, "ready_partition": "t-1（昨日）",
                 "note": "样例库由 seed.py 生成，分区就绪约定为 T+1；真实接入后由元数据平台提供"}

    # ── 统一入口 ─────────────────────────────────────────────
    def search(self, query: str) -> dict:
        """按查询词返回结构化元数据结果（表/指标/血缘/就绪）。"""
        q = query.strip()
        if not q:
            return {"query": q, "tables": [], "metrics": [], "lineage": None,
                    "note": "查询词为空"}
        # 精确表名优先
        table = self.table_info(q)
        tables = [table] if table else self.find_tables(q)
        metrics = self.find_metrics(q)
        lineage = self.lineage(q) if self.table_info(q) else None
        readiness = self.readiness(q) if self.table_info(q) else None
        # 无命中时给候选（表/指标清单）
        note = ""
        if not tables and not metrics:
            note = (f"未命中：{q}。候选表: {self.all_tables()[:8]}；"
                    f"候选指标: {sorted(self.onto.functions)[:8]}")
        return {"query": q, "tables": tables, "metrics": metrics,
                "lineage": lineage, "readiness": readiness, "note": note}

    def all_tables(self) -> list[str]:
        tables = set()
        for o in self.onto.objects.values():
            for t in o.get("source_tables", []):
                tables.add(t["table"])
        return sorted(tables)
