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

from pathlib import Path
from typing import Any

from .ontology_loader import Ontology
from .table_metadata import LINEAGE  # 血缘单一数据源（table_metadata 采集入库用同一份）

# 层 → 中文名
LAYER_CN = {"ODS": "贴源层", "DWD": "明细层", "DWS": "汇总层",
            "ADS": "应用层", "DIM": "维度层"}


class MetadataService:
    """元数据检索：表/指标/血缘/就绪。

    数据源优先级：
      1. ontology_tables（Supabase 真源，多人维护的表元数据）——tables_store 配置时查这里
      2. 本地回落：Ontology（source_tables）+ 内置血缘逻辑（未配置 Supabase 或表不存在时）
    """

    def __init__(self, onto: Ontology, db_path: str | Path | None = None,
                 tables_store: Any = None):
        self.onto = onto
        self.db_path = Path(db_path) if db_path else None
        self.tables_store = tables_store  # 查 ontology_tables 的 client（可选）

    # ── 表检索 ──────────────────────────────────────────────
    def _table_from_store(self, table: str) -> dict | None:
        """从 ontology_tables 查单表（Supabase 真源）。未配置/表不存在返回 None。"""
        if self.tables_store is None:
            return None
        try:
            return self.tables_store.table_info(table)
        except Exception:  # noqa: BLE001 —— 库不可用回落后端
            return None

    def table_info(self, table: str) -> dict | None:
        """按物理表名查表信息。优先 ontology_tables，回落 Ontology。"""
        table = table.lower()
        # ① 库优先（多人维护的表元数据）
        from_store = self._table_from_store(table)
        if from_store is not None:
            return from_store
        # ② 回落 Ontology（本地推导）
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
                        "domain": o.get("domain", "unknown"),
                        "object_type": o.get("object_type", "fact"),
                        "object_status": o.get("status", "active"),
                        "data_owner": o.get("data_owner", ""),
                        "granularities": t.get("granularities", []),
                        "joinable": t.get("joinable", False),
                        "authority": t.get("authority"),
                        "perm_column": t.get("perm_column"),
                        "fields": props,
                        "pre_aggregated": list((t.get("pre_aggregated") or {}).keys()),
                        "description": o.get("description", ""),
                    }
        return None

    def _query_tokens(self, keyword: str) -> list[str]:
        """查询词分词：中文连续串拆 2-gram + 英文 token + 整句。

        "消费订单的金额" → ["消费","费订","订单","单的","的金","金额"]（2-gram）
        + 整句本身。停用词在 2-gram 后天然稀释，保留全部（够召回即可）。
        """
        import re as _re
        kw = keyword.strip().lower()
        if not kw:
            return []
        tokens: list[str] = [kw]
        for s in _re.findall(r"[\u4e00-\u9fff]+", kw):
            for i in range(len(s) - 1):
                tokens.append(s[i:i+2])
        tokens += _re.findall(r"[a-z0-9_]+", kw)
        return tokens

    def find_tables(self, keyword: str) -> list[dict]:
        """按关键字找表（表名 / 对象名 / 展示名 / 描述模糊匹配）。

        支持整句与 2-gram 分词：查询词"退款 就绪时间"/"消费订单的金额"
        任一 token 命中即召回（自然语言句子场景）。"""
        tokens = self._query_tokens(keyword)
        hits = []
        for obj_name, o in self.onto.objects.items():
            for t in o.get("source_tables", []):
                hay = " ".join([t["table"], obj_name, o.get("display_name", ""),
                                o.get("description", ""), t.get("layer", "")]).lower()
                if any(tok in hay for tok in tokens):
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
            "domain": fn.get("domain", "unknown"),
            "category": fn.get("category", ""),
            "status": fn.get("status", "active"),
            "data_owner": fn.get("data_owner", ""),
            "unit": fn.get("unit", ""),
            "version": fn.get("version"),
            "family": fn.get("family"),
            "variant_label": fn.get("variant_label"),
            "default_of_family": fn.get("default_of_family", False),
            "required_filters": fn.get("required_filters", []),
            "supported_dimensions": fn.get("supported_dimensions", []),
            "do_not": fn.get("do_not", ""),
        }

    def find_metrics(self, keyword: str) -> list[dict]:
        """按关键字找指标（名称/展示名/描述/族/口径词）。

        支持整句与 2-gram 分词（同 find_tables）。"""
        tokens = self._query_tokens(keyword)
        hits = []
        for fn in self.onto.functions.values():
            hay = " ".join([fn["name"], fn.get("display_name", ""),
                            fn.get("description", ""), fn.get("family", ""),
                            fn.get("variant_label", "")]).lower()
            if any(tok in hay for tok in tokens):
                info = self.metric_info(fn["name"])
                if info:
                    hits.append(info)
        return hits

    # ── 血缘 / 加工逻辑 ──────────────────────────────────────
    def lineage(self, table: str) -> dict | None:
        """查表的加工逻辑（血缘）：来源表 + 加工说明。

        优先 ontology_tables（Supabase 真源），回落本地内置血缘。
        """
        table = table.lower()
        from_store = self._table_from_store(table)
        if from_store and from_store.get("lineage", {}).get("source"):
            lin = from_store["lineage"]
            return {"table": table, "source": lin.get("source", ""),
                    "logic": lin.get("logic", "")}
        if table in LINEAGE:
            return {"table": table, **LINEAGE[table]}
        # DWD 明细无上游加工（贴源），返回空
        for obj_name, o in self.onto.objects.items():
            for t in o.get("source_tables", []):
                if t["table"] == table and t.get("layer") == "DWD":
                    return {"table": table, "source": "ODS/上游系统（样例库由 seed 直接生成）",
                            "logic": "明细层：清洗后原子事实，无聚合加工"}
        return None

    # ── 就绪时间 ─────────────────────────────────────────────
    def readiness(self, table: str) -> dict:
        """表就绪时间：优先 ontology_tables（真源维护），回落本地约定 t-1。

        缺陷②修复：readiness 值统一字符串兜底（None → 空串），
        上游可空字段 field or '' 处理，杜绝 join 序列混入 NoneType。
        """
        table = table.lower()
        from_store = self._table_from_store(table)
        store_val = (from_store or {}).get("readiness")
        if from_store and store_val:
            return {"table": table,
                    "ready_partition": str(store_val) or "",
                    "note": "来自 ontology_tables（Supabase 真源维护）"}
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
        # 缺陷②：readiness 永不返回 None（无命中时给空 dict 而非 None）
        readiness = self.readiness(q) if self.table_info(q) else {"table": q, "ready_partition": "", "note": ""}
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
