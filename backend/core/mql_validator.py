"""MQL 校验器：schema 校验 + 物理渗入检测（安全边界）。

任何物理表名/字段名（dwd_/dws_/ads_/dim_ 前缀）渗入 MQL 一律拒绝——
这是幻觉防控的第一道闸门，LLM 只能输出纯业务语义。
"""
from __future__ import annotations

import re

from .mql_schema import GRANULARITIES, OPERATORS, PHYSICAL_RE
from .ontology_loader import Ontology


class MqlValidator:
    def __init__(self, ontology: Ontology):
        self.onto = ontology

    def validate(self, mql: dict) -> dict:
        errors: list[str] = []
        warnings: list[str] = []

        if not isinstance(mql, dict):
            return {"ok": False, "errors": ["MQL 必须是对象"], "warnings": []}

        # 1. 物理渗入检测（安全边界，最优先）：物理表名（前缀）+ 物理列名（Ontology 精确集合）
        raw = str(mql)
        if PHYSICAL_RE.search(raw):
            errors.append("MQL 中出现物理表名（如 dwd_xxx / ads_xxx），违反纯业务语义约束。"
                          "请使用 Ontology 业务属性名。")
        for pname in self.onto.physical_names:
            if re.search(rf"\b{re.escape(pname)}\b", raw):
                errors.append(f"MQL 中出现物理字段名 {pname}，违反纯业务语义约束。"
                              "请使用 Ontology 业务属性名。")
                break

        # 2. 指标
        metrics = mql.get("metrics") or ([mql["metric"]] if mql.get("metric") else None)
        if not metrics:
            errors.append("缺少 metrics（或 v1.0 的 metric）")
        else:
            for m in metrics:
                name = m.get("name") if isinstance(m, dict) else m
                if name not in self.onto.functions:
                    errors.append(f"指标 {name} 未注册，候选：{sorted(self.onto.functions)}")

        # 3. 维度
        td = self.onto.time_dim
        for d in mql.get("dimensions", []):
            name = d.get("name")
            if name == td:
                gran = d.get("granularity", "day")
                if gran not in GRANULARITIES:
                    errors.append(f"时间粒度 {gran} 非法，应为 {sorted(GRANULARITIES)}")
            elif name not in self.onto.property_owner:
                errors.append(f"维度 {name} 不是已注册的业务属性")

        # 4. 过滤
        for f in mql.get("filters", []):
            field = f.get("field")
            if field not in self.onto.property_owner:
                errors.append(f"过滤字段 {field} 不是已注册的业务属性")
            if f.get("operator") not in OPERATORS:
                errors.append(f"过滤操作符 {f.get('operator')} 非法，应为 {sorted(OPERATORS)}")
            if "value" not in f:
                errors.append(f"过滤字段 {field} 缺少 value")

        # 5. 时间完整性：有时间维度必须有 time_range（缺省则由翻译引擎按 t-1 兜底，记 warning）
        has_time_dim = any(d.get("name") == self.onto.time_dim for d in mql.get("dimensions", []))
        if has_time_dim and "time_range" not in mql:
            warnings.append("未识别到时间参数，翻译引擎将按默认 t-1 处理")

        # 6. 排序/限量
        for s in mql.get("sort", []):
            if s.get("field") not in {m.get("name") for m in (metrics or []) if isinstance(m, dict)} | \
               {d.get("name") for d in mql.get("dimensions", [])} | {self.onto.time_dim}:
                errors.append(f"排序字段 {s.get('field')} 非法")
        if mql.get("limit") is not None and not isinstance(mql.get("limit"), int):
            errors.append("limit 必须是整数")

        return {"ok": not errors, "errors": errors, "warnings": warnings}
