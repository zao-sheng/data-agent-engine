"""探索 SQL → 口径提取 → 固化注册预填（P2：探索稳定后固化为正式指标）。

背景：探索性取数（路径 E）观测稳定后，需固化为标准指标/维度。
本模块把探索 SQL 的 SELECT/WHERE/GROUP BY/FROM 反推为候选口径：

  * SELECT 聚合表达式        → 候选指标公式（SUM(x)/COUNT(DISTINCT y)）
  * GROUP BY 列             → 候选维度（业务属性名）
  * WHERE 非 dt 条件         → 候选 required_filters（如 is_valid = 1）
  * FROM/JOIN 表            → 候选 source_tables（物理表名，需已注册）

输出为 modeling_plan 的 register 变更项草稿（可编辑），用户确认后走
既有路径 D（modeling_plan → ontology_register），复用成熟治理流程。
物理列 → 业务属性：优先反查 Ontology field_mapping（列属于哪个对象），
反查不到保留物理列名并标注（人工补 display_name/description）。

约束：
  * 只提取口径，不自动注册——生成的是「草稿」，必须经用户确认
  * 公式白名单与翻译引擎一致（FORMULA_FNS）
  * 提取结果尽量保守：识别不了的部分标注待人工确认，不臆造
"""
from __future__ import annotations

import re

from .mql_schema import FORMULA_FNS, IDENT_RE, PHYSICAL_RE
from .ontology_loader import Ontology

# 聚合函数（含可选的 DISTINCT 参数）
AGG_RE = re.compile(r"\b(SUM|COUNT|AVG|MAX|MIN)\s*\(\s*(?:DISTINCT\s+)?([A-Za-z_][A-Za-z0-9_.]*)", re.I)
# 非 dt 的 WHERE 条件（粗提取：xx op value 或 xx IS NOT NULL 等）
WHERE_COND_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*)\s*(=|<|>|<=|>=|!=|<>|LIKE|IN|IS\s+NOT\s+NULL|BETWEEN)", re.I)
# 分组列（GROUP BY 后的列名，到 ORDER BY/LIMIT/结尾为止）
GROUP_RE = re.compile(r"GROUP\s+BY\s+(.+?)(?:\s+ORDER\s+BY\s|\s+LIMIT\s|$)", re.I | re.S)


class PromoteError(Exception):
    """口径提取失败（信息可直接返回）。"""


def _extract_select_metrics(sql: str) -> list[dict]:
    """提取 SELECT 中的聚合表达式 → 候选指标。

    返回 [{name, formula, agg, column}]；name 为建议指标名（snake_case）。
    """
    metrics: list[dict] = []
    # 简化：取 SELECT 与 FROM 之间的文本
    m = re.search(r"SELECT\s+(.+?)\s+FROM", sql, re.I | re.S)
    if not m:
        return metrics
    select_block = m.group(1)
    for agg_match in AGG_RE.finditer(select_block):
        fn = agg_match.group(1).upper()
        col = agg_match.group(2)
        # 去表别名前缀：SUM(F.pay_amt) → SUM(pay_amt)（固化公式不含物理别名）
        col_clean = col.split(".")[-1]
        formula = f"{fn}({col_clean})"
        # 建议名：agg + 列名（去表前缀），如 SUM(pay_amt) → pay_amt_sum
        col_short = col.split(".")[-1]
        if fn == "COUNT" and col.upper() == "*":
            name = "count_all"
        elif fn == "COUNT" and "DISTINCT" in agg_match.group(0).upper():
            name = f"{col_short}_distinct_count"
        else:
            name = f"{col_short}_{fn.lower()}"
        metrics.append({"name": name, "formula": formula,
                        "agg": fn, "column": col_short})
    return metrics


def _extract_group_dims(sql: str) -> list[str]:
    """提取 GROUP BY 列 → 候选维度（去表前缀，去序号）。"""
    m = GROUP_RE.search(sql)
    if not m:
        return []
    raw = m.group(1).strip()
    dims = []
    for part in raw.split(","):
        tok = part.strip().split()[-1]  # 去掉 ASC/DESC/别名
        tok = tok.split(".")[-1]        # 去表前缀：S.store_type → store_type
        if tok and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", tok):
            dims.append(tok)
    return dims


def _extract_where_filters(sql: str) -> list[str]:
    """提取 WHERE 非 dt 条件 → 候选 required_filters。

    仅提取形如 `col = value` / `col IS NOT NULL` 的简单条件（dt 分区除外）。
    """
    m = re.search(r"WHERE\s+(.+?)(?:\s+(?:GROUP\s+BY|ORDER\s+BY|LIMIT|$))", sql, re.I | re.S)
    if not m:
        return []
    where_block = m.group(1)
    # 按 AND 粗切（忽略子查询/括号内的复杂条件，保守提取）
    conds = re.split(r"\s+AND\s+", where_block, flags=re.I)
    filters = []
    for c in conds:
        c = c.strip()
        cm = WHERE_COND_RE.search(c)
        if not cm:
            continue
        col = cm.group(1)
        if col.lower() in ("dt", "f.dt"):
            continue  # 分区条件不固化为 required_filter
        # 简单 `col = value` 且 value 是字面量 → 保留原样（is_valid = 1）
        if re.search(r"=\s*['\"]?[A-Za-z0-9_.]+['\"]?\s*$", c, re.I):
            # 去表别名前缀：F.is_valid = 1 → is_valid = 1（固化条件不含物理别名）
            c = re.sub(r"^\s*[A-Za-z_][A-Za-z0-9_]*\.", "", c.strip())
            filters.append(c)
    return filters


def _resolve_column(onto: Ontology, column: str) -> dict:
    """物理列 → 业务属性（反查 Ontology field_mapping）。

    返回 {owner_object, property_name, physical_column}；
    反查不到 → owner_object=None（保留物理列名，人工补）。
    """
    column = column.lower()
    for obj_name, o in onto.objects.items():
        for t in o.get("source_tables", []):
            fm = t.get("field_mapping", {})
            for prop, col in fm.items():
                if str(col).lower() == column:
                    return {"owner_object": obj_name, "property_name": prop,
                            "physical_column": column}
    return {"owner_object": None, "property_name": None,
            "physical_column": column}


def extract_metric_definition(onto: Ontology, sql: str) -> dict:
    """从探索 SQL 提取指标口径草稿。

    返回 {sql, metrics[], dimensions[], required_filters[], source_tables[],
           unmapped_columns[], review_required}
    """
    if not sql or not sql.strip():
        raise PromoteError("SQL 为空，无法提取口径")

    # 源表：SQL 引用的物理表（须已注册；未注册提示先建表）
    from .explore import extract_tables
    tables = extract_tables(sql)
    registered = candidate_tables(onto)
    source_tables = [t for t in tables if t in registered]
    unregistered = [t for t in tables if t not in registered]

    # 指标（聚合表达式）
    metrics = _extract_select_metrics(sql)
    # 维度（GROUP BY）
    dims = _extract_group_dims(sql)
    # 过滤（WHERE 非 dt）
    filters = _extract_where_filters(sql)

    # 列归属解析（物理列 → 业务属性）
    unmapped: list[str] = []
    for m in metrics:
        info = _resolve_column(onto, m["column"])
        m.update(info)
        if info["owner_object"] is None and m["column"] not in ("*",):
            unmapped.append(m["column"])
    for d in dims:
        info = _resolve_column(onto, d)
        if info["owner_object"] is None:
            unmapped.append(d)

    return {
        "sql": sql,
        "metrics": metrics,
        "dimensions": dims,
        "required_filters": filters,
        "source_tables": source_tables,
        "unregistered_tables": unregistered,
        "unmapped_columns": sorted(set(unmapped)),
        "review_required": True,
    }


def build_register_draft(onto: Ontology, sql: str) -> dict:
    """探索 SQL → modeling_plan 的 register 变更项草稿。

    生成两个可编辑草稿：
      1. function 草稿（指标：name/formula/owner/required_filters/supported_dimensions）
      2. object 草稿（对象：name/source_tables/domain——若源对象未注册）
    返回 {ok, changes[], errors[]}；changes 可直接喂给 modeling_plan。
    """
    try:
        extracted = extract_metric_definition(onto, sql)
    except PromoteError as e:
        return {"ok": False, "changes": [], "errors": [str(e)]}

    changes: list[dict] = []
    errors: list[str] = []

    # 1. 每个指标 → function 注册草稿
    for m in extracted["metrics"]:
        owner = m.get("owner_object") or (extracted["source_tables"][0] if
                                          extracted["source_tables"] else "")
        # 公式必须用业务属性名（翻译引擎靠 field_mapping 映射回物理列）：
        # 物理列反查到属性 → 公式用属性；反查不到 → 保留物理列并标注待补
        col_short = m["column"]
        prop = m.get("property_name")
        formula = f"{m['agg']}({prop if prop else col_short})"
        # 草稿构造：空值字段省略（create 校验要求非空；DB 落默认由 schema 保证）
        entry = {
            "name": m["name"],
            "display_name": m["name"],
            "description": f"由探索 SQL 提取的候选指标（口径待人工确认）：{formula}",
            "formula": formula,
            "owner": owner,
            "domain": onto.domain_of(owner) if owner in onto.objects else "ord",
            "category": "探索",
            "status": "draft",
            "required_filters": extracted["required_filters"],
            "supported_dimensions": extracted["dimensions"],
            "supported_granularities": ["day"],
            "tags": [],
        }
        if prop:
            entry["description"] += f"（对应业务属性 {prop}）"
        else:
            entry["description"] += "（⚠️ 物理列未映射业务属性，需人工补 field_mapping）"
        fn_draft = {
            "type": "register",
            "obj_name": m["name"],
            "kind": "function",
            "entry": entry,
            "note": "探索固化草稿（draft）：公式/归属/过滤来自探索 SQL，人工确认后激活",
            "editable": ["display_name", "formula", "owner", "required_filters",
                         "supported_dimensions", "supported_granularities", "domain"],
        }
        changes.append(fn_draft)

    # 2. 未注册源表 → object 注册草稿
    for t in extracted["unregistered_tables"]:
        obj_draft = {
            "type": "register",
            "obj_name": t,
            "kind": "object",
            "entry": {
                "name": t,
                "display_name": t,
                "description": f"由探索 SQL 引用的未注册表（候选对象，待人工补描述）",
                "domain": "ord",
                "object_type": "fact",
                "status": "draft",
                "security_level": "L2",
                "update_frequency": "T+1",
                "aliases": [],
                "required_filters": extracted["required_filters"],
                "tags": [],
                "properties": [],
                "source_tables": [{
                    "table": t, "layer": "DWD", "authority": "gold",
                    "joinable": True, "granularities": ["day"],
                    "available_dims": [], "field_mapping": {},
                }],
                "versions": [],
                "default_version": None,
            },
            "note": "探索引用的未注册表：需先建表（路径 D create）或补充映射后注册",
            "editable": ["display_name", "domain", "object_type", "source_tables"],
        }
        changes.append(obj_draft)

    if extracted["unmapped_columns"]:
        errors.append(f"以下物理列未映射到业务属性（人工补 field_mapping）: "
                      f"{extracted['unmapped_columns']}")

    return {"ok": True, "changes": changes, "errors": errors,
            "extracted": extracted, "review_required": True}


# 复用 explore.candidate_tables（避免循环 import 放模块级）
def candidate_tables(onto: Ontology) -> set[str]:
    from .explore import candidate_tables as _ct
    return _ct(onto)
