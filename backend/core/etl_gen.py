"""Spark SQL ETL 脚本生成器（路径 D：ETL 管道草稿）。

输入：目标表（layer/domain/subject/目标对象）+ 指标 + 可选维度 + 可选过滤。
输出：Spark SQL INSERT OVERWRITE 聚合脚本（从源明细表聚合到目标表）。

设计约束：
  * 源表选择：优先目标对象同 owner 的 DWD 明细表（ETL 是聚合语义，源必须是明细）；
    跨域指标用 CTE + FULL OUTER JOIN（与翻译引擎多指标策略一致）。
  * 指标公式：复用 Ontology formula（白名单函数），字段名经 field_mapping 落到物理列。
  * 必过滤：required_filters 自动注入 WHERE。
  * 分区：目标表按 dt 分区，INSERT OVERWRITE 带 PARTITION(dt)。
  * 生成后需人工 Review + 审批（路径 D 阶段 3/4）。
"""
from __future__ import annotations

import re

from .ddl_gen import table_name
from .mql_schema import FORMULA_FNS, IDENT_RE
from .ontology_loader import Ontology

PROP_RE = IDENT_RE  # 兼容别名（公式/条件 token 提取，同一正则）


class EtlGenError(Exception):
    pass


def _compile_metric(fn: dict, field_map: dict[str, str]) -> str:
    """指标公式 → 物理列表达式：SUM(pay_amount) → SUM(F.pay_amt)。"""
    formula = fn["formula"]
    # 公式里的属性名 → 物理列
    for prop, col in field_map.items():
        formula = re.sub(rf"\b{re.escape(prop)}\b", f"F.{col}", formula)
    # 白名单校验：非 F.xxx / 函数 / 数字 / 运算符 的标识符拒绝
    cleaned = re.sub(r"F\.[A-Za-z_][A-Za-z0-9_]*", "", formula)
    for tok in PROP_RE.findall(cleaned):
        if tok not in FORMULA_FNS:
            raise EtlGenError(f"公式含非法标识符: {tok}（{fn['name']}）")
    return formula


def _required_filters(onto: Ontology, fn: dict) -> list[str]:
    """指标 owner 的 required_filters 并集。"""
    return [f for f in fn.get("required_filters", [])]


def generate_etl(onto: Ontology, obj_name: str, layer: str, domain: str | None = None,
                 subject: str | None = None,
                 metrics: list[str] | None = None,
                 dimensions: list[str] | None = None) -> dict:
    """生成 Spark SQL ETL 草稿（INSERT OVERWRITE 聚合）。

    obj_name: 目标对象（决定目标表命名 + 源表归属）
    layer/domain/subject: 目标表 {layer}_{domain}_{subject}{后缀}
    metrics: 指标名列表（Ontology functions 中已注册；缺省取目标对象全部指标）
    dimensions: 分组维度（业务属性名，须在源表 available_dims 或可 JOIN）
    domain 缺省取对象自身业务域（与本体治理字段对齐，不再硬编码 ord）。
    """
    o = onto.get_object(obj_name)
    if not o:
        return {"error": f"对象 {obj_name} 不存在"}
    layer = layer.upper()
    domain = domain or o.get("domain") or "ord"
    subject = subject or obj_name.lower()
    target = table_name(layer, domain, subject)

    # 源表：目标对象同 owner 的 DWD 明细表（ETL 聚合语义要求源为明细）；
    # 跳过 status=deprecated 的废弃表（与翻译引擎表选择一致）
    source_tables = o.get("source_tables", [])
    detail = [t for t in source_tables
              if t.get("layer") == "DWD" and t.get("status") != "deprecated"]
    if not detail:
        return {"error": f"对象 {obj_name} 没有可用的 DWD 明细源表（或全部已废弃），无法生成 ETL（需先建明细表）"}
    src = detail[0]
    src_table = src["table"]
    field_map = src.get("field_mapping", {})

    # 指标：缺省取 owner 对象上注册的全部指标
    if not metrics:
        metrics = [f["name"] for f in onto.functions.values() if f.get("owner") == obj_name]
    if not metrics:
        return {"error": f"对象 {obj_name} 没有可聚合的已注册指标，请先在 Ontology 注册"}
    fns = []
    for m in metrics:
        fn = onto.get_function(m)
        if fn is None:
            return {"error": f"指标 {m} 未注册"}
        fns.append(fn)

    # 维度列（业务属性 → 物理列；源表 field_mapping 覆盖不到则报错）
    dims = dimensions or []
    dim_cols: list[tuple[str, str]] = []
    for d in dims:
        col = field_map.get(d)
        if not col:
            return {"error": f"维度 {d} 不在源表 {src_table} 的 field_mapping 中"}
        dim_cols.append((d, col))

    # SELECT 列：分区列 + 指标聚合 + 维度
    part_col = onto.partition_col
    selects = [f"  F.{part_col}"]
    for fn in fns:
        try:
            selects.append(f"  {_compile_metric(fn, field_map)} AS {fn['name']}")
        except EtlGenError as e:
            return {"error": str(e)}
    group_cols = [f"F.{part_col}"] + [f"F.{col}" for _, col in dim_cols]
    for _, col in dim_cols:
        selects.append(f"  F.{col} AS {col}")

    # WHERE：required_filters（并集，映射到物理列，统一加 F. 前缀）
    reqs: list[str] = []
    seen = set()
    for fn in fns:
        for rf in _required_filters(onto, fn):
            if rf not in seen:
                seen.add(rf)
                reqs.append(rf)
    where = []
    for rf in reqs:
        # rf 形如 "is_valid = 1"：字段名映射到物理列（缺省用原名，单表无歧义）
        m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*(.*)", rf)
        if m:
            prop, rest = m.group(1), m.group(2)
            col = field_map.get(prop, prop)
            where.append(f"F.{col} {rest}")
    where_sql = f"\nWHERE {' AND '.join(where)}" if where else ""

    group_sql = f"\nGROUP BY {', '.join(group_cols)}" if group_cols else ""

    # 目标表分区列 = 源表分区列（同一 dt 约定）
    etl = (f"-- Spark SQL ETL（目标引擎 Spark，Hive 风格）\n"
           f"-- 源: {src_table} → 目标: {target}（粒度: 日）\n"
           f"INSERT OVERWRITE TABLE {target} PARTITION ({part_col})\n"
           f"SELECT\n" + ",\n".join(selects) +
           f"\nFROM {src_table} F{where_sql}{group_sql};\n")

    return {"target": target, "source": src_table, "engine": "spark",
            "metrics": metrics, "dimensions": dims,
            "etl": etl, "review_required": True}
