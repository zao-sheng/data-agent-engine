"""本体注册条目校验器（缺陷①修复）：ontology_register 写入前的结构校验。

背景：register 的 entry 是 YAML 形状 dict，写入前无结构校验 → 非法值入库后
直到 reload 才爆炸（如 pre_aggregated: false 被接受，reload 解析时调
.values() 报 'bool' object has no attribute 'values'），且错误为裸 HTTP 400。

本模块按 kind（object/function/relation/glossary/config）校验 entry 字段
格式与取值，失败返回字段级错误：字段 <X> 格式错误：期望 <类型>，收到 <实际值>。

字段规则来源：backend/supabase/schema.sql 各列 COMMENT（权威参考）+
backend/ontology/*.yaml（entry 结构权威样本）+ 交接文档验收标准。
"""
from __future__ import annotations

from typing import Any

from .mql_schema import FORMULA_FNS, GRANULARITIES, IDENT_RE

# ── 枚举/取值域（与 schema.sql COMMENT 一致）─────────────────
OBJECT_TYPES = {"fact", "dim"}
STATUSES = {"active", "draft", "deprecated"}
SECURITY_LEVELS = {"L1", "L2", "L3"}
UPDATE_FREQUENCIES = {"T+1", "小时", "实时"}
PROPERTY_TYPES = {"string", "decimal", "date", "int", "integer", "bigint"}
LAYERS = {"ODS", "DWD", "DWS", "ADS", "DIM"}  # 与 metadata.LAYER_CN / mql_schema.PHYSICAL_RE 一致
AUTHORITIES = {"gold", "silver", "bronze"}
GLOSSARY_TYPES = {"object", "property", "metric"}
CARDINALITIES = {"N:1", "1:N", "1:1"}


class EntryValidationError(Exception):
    """条目校验失败：message 为字段级错误（可直接返回给调用方）。"""


def _type_name(v: Any) -> str:
    if isinstance(v, bool):
        return "布尔值"
    if isinstance(v, dict):
        return "字典"
    if isinstance(v, list):
        return "列表"
    if isinstance(v, str):
        return "字符串"
    return type(v).__name__


def _expect_type(entry: dict, field: str, expected: str, check, required: bool = True) -> None:
    """校验字段存在性 + 类型；失败抛 EntryValidationError。

    required=False 且字段缺失/为 None → 跳过（合并更新语义：省略字段保留原值）。
    """
    if field not in entry or entry[field] is None:
        if required:
            raise EntryValidationError(f"字段 {field} 缺失（必填）")
        return
    v = entry[field]
    if not check(v):
        raise EntryValidationError(
            f"字段 {field} 格式错误：期望 {expected}，收到 {_type_name(v)}（{v!r}）")


def _expect_enum(entry: dict, field: str, allowed: set[str],
                 required: bool = True, default: str = "", mode: str = "create") -> None:
    """校验枚举取值。

    create 模式：缺省按 default 补入（落库默认值，与 schema DEFAULT 一致）。
    update 模式：不补默认（合并语义：省略字段保留原值，避免覆盖已有值）。
    """
    if field not in entry or entry[field] is None:
        if required:
            raise EntryValidationError(f"字段 {field} 缺失（必填）")
        if default and mode == "create":
            entry[field] = default
        return
    v = entry[field]
    if v not in allowed:
        raise EntryValidationError(
            f"字段 {field} 取值非法：期望 {sorted(allowed)} 之一，收到 {v!r}")


def _expect_list_of_str(entry: dict, field: str, required: bool = True) -> None:
    def _is_str_list(v):
        return isinstance(v, list) and all(isinstance(x, str) for x in v)
    _expect_type(entry, field, "字符串列表（如 ['is_valid = 1']）", _is_str_list, required)


def _expect_dict(entry: dict, field: str, required: bool = True) -> None:
    def _is_dict(v):
        return isinstance(v, dict) and not isinstance(v, bool)
    _expect_type(entry, field, "字典（如 {'refund_count': 'refund_cnt'}）", _is_dict, required)


# ── 各 kind 校验器 ──────────────────────────────────────────

def _validate_function(entry: dict, mode: str = "create") -> None:
    """function 字段规则（见交接文档 + schema.sql ontology_functions）。

    mode=create：name/formula/owner 等必填；mode=update：只校验传入字段
    （合并语义：省略字段保留原值，function 更新须带 formula/owner 由 DB
    NOT NULL 约束兜底，此处仍做存在性提示）。
    """
    # 唯一键：两种模式都必填
    _expect_type(entry, "name", "字符串", lambda v: isinstance(v, str) and v.strip(), required=True)
    # 核心必填（create 全量 / update 只校验传入）：
    # display_name/description/formula/owner/domain 是指标可用的最小集
    for f in ("display_name", "description", "formula", "owner", "domain"):
        _expect_type(entry, f, "字符串", lambda v: isinstance(v, str) and v.strip(),
                     required=(mode == "create"))
    # 治理字段（category/data_owner/unit）：可选——探索固化草稿（draft）阶段
    # 可省略，人工确认时补；DB 有默认值（''）
    for f in ("category", "data_owner", "unit"):
        if f in entry and entry[f] is not None:
            _expect_type(entry, f, "字符串", lambda v: isinstance(v, str), required=False)
    # 枚举/默认（update 模式不补默认，避免覆盖原值）
    _expect_enum(entry, "status", STATUSES, required=False, default="active", mode=mode)
    _expect_type(entry, "version", "字符串（默认 v1.0）",
                 lambda v: isinstance(v, str), required=False)
    if "version" not in entry or not entry.get("version"):
        if mode == "create":
            entry["version"] = "v1.0"
    # 布尔
    _expect_type(entry, "default_of_family", "布尔值",
                 lambda v: isinstance(v, bool), required=False)
    if "default_of_family" not in entry:
        if mode == "create":
            entry["default_of_family"] = False
    # 列表
    for f in ("required_filters", "supported_dimensions", "supported_granularities", "tags"):
        _expect_list_of_str(entry, f, required=False)
    # 条件可选
    for f in ("family", "variant_label", "do_not"):
        if f in entry and entry[f] is not None:
            _expect_type(entry, f, "字符串", lambda v: isinstance(v, str), required=False)
    # formula 白名单：仅白名单函数（SUM/COUNT/AVG/MAX/MIN/DISTINCT）+ 属性名
    # 简单校验：剥离函数调用后的标识符 token 必须都是白名单函数或小写属性名。
    # （深度 AST 校验由翻译引擎 _compile_formula 执行，这里拦截明显非法值。）
    formula = entry.get("formula", "")
    if formula is None:
        if mode == "create":
            raise EntryValidationError("字段 formula 缺失或为空（必填）")
    elif not (isinstance(formula, str) and formula.strip()):
        raise EntryValidationError("字段 formula 缺失或为空（必填）")
    elif isinstance(formula, str):
        import re as _re
        tokens = _re.findall(IDENT_RE, formula)
        for tok in tokens:
            if tok.upper() in FORMULA_FNS:
                continue
            if tok == tok.lower():  # 属性名（小写）放行，交由翻译引擎做存在性校验
                continue
            raise EntryValidationError(
                f"字段 formula 含非法标识符 {tok!r}：仅允许白名单函数 "
                f"{sorted(FORMULA_FNS)} + 属性名")
    # owner 归属对象存在性：由调用方（有 Ontology 上下文）校验


def _validate_object(entry: dict, mode: str = "create") -> None:
    """object 字段规则（见交接文档 + schema.sql ontology_objects）。

    mode=create：name/display_name 等必填；mode=update：只校验传入字段
    （合并语义：Region 只提交 {name, display_name} 可合法更新）。
    """
    # 唯一键：两种模式都必填
    _expect_type(entry, "name", "字符串", lambda v: isinstance(v, str) and v.strip(), required=True)
    for f in ("display_name", "description", "domain", "data_owner"):
        _expect_type(entry, f, "字符串", lambda v: isinstance(v, str) and v.strip(),
                     required=(mode == "create"))
    _expect_enum(entry, "object_type", OBJECT_TYPES, required=False, default="fact", mode=mode)
    _expect_enum(entry, "status", STATUSES, required=False, default="active", mode=mode)
    _expect_enum(entry, "security_level", SECURITY_LEVELS, required=False, default="L2", mode=mode)
    _expect_enum(entry, "update_frequency", UPDATE_FREQUENCIES, required=False, default="T+1", mode=mode)
    for f in ("aliases", "required_filters", "tags"):
        _expect_list_of_str(entry, f, required=False)

    # properties: list[{name, type, unit?, aliases?, description}]
    if "properties" in entry and entry["properties"] is not None:
        _expect_type(entry, "properties", "属性列表（元素含 name/type）",
                     lambda v: isinstance(v, list), required=False)
        for i, p in enumerate(entry["properties"]):
            if not isinstance(p, dict):
                raise EntryValidationError(f"字段 properties[{i}] 格式错误：期望字典，收到 {_type_name(p)}")
            if not isinstance(p.get("name"), str) or not p["name"].strip():
                raise EntryValidationError(f"字段 properties[{i}].name 缺失或为空（必填）")
            if p.get("type") is not None and str(p["type"]).lower() not in PROPERTY_TYPES:
                raise EntryValidationError(
                    f"字段 properties[{i}].type 取值非法：期望 {sorted(PROPERTY_TYPES)} 之一，收到 {p['type']!r}")

    # source_tables: list[{table, layer, authority, joinable, granularities,
    #                      available_dims, field_mapping, pre_aggregated?...}]
    if "source_tables" in entry and entry["source_tables"] is not None:
        _expect_type(entry, "source_tables", "物理表映射列表",
                     lambda v: isinstance(v, list), required=False)
        for i, t in enumerate(entry["source_tables"]):
            if not isinstance(t, dict):
                raise EntryValidationError(f"字段 source_tables[{i}] 格式错误：期望字典，收到 {_type_name(t)}")
            for f in ("table", "layer"):
                if not isinstance(t.get(f), str) or not t[f].strip():
                    raise EntryValidationError(f"字段 source_tables[{i}].{f} 缺失或为空（必填）")
            if t.get("layer") is not None and t["layer"].upper() not in LAYERS:
                raise EntryValidationError(
                    f"字段 source_tables[{i}].layer 取值非法：期望 {sorted(LAYERS)}，收到 {t['layer']!r}")
            if t.get("authority") is not None and t["authority"].lower() not in AUTHORITIES:
                raise EntryValidationError(
                    f"字段 source_tables[{i}].authority 取值非法：期望 {sorted(AUTHORITIES)}，收到 {t['authority']!r}")
            # 关键：field_mapping / pre_aggregated 必须为 dict，布尔/字符串必须 400
            _expect_dict(t, "field_mapping", required=True)
            if "pre_aggregated" in t and t["pre_aggregated"] is not None:
                _expect_dict(t, "pre_aggregated", required=False)
            if t.get("granularities") is not None:
                _expect_type(t, "granularities", "粒度列表",
                             lambda v: isinstance(v, list) and all(x in GRANULARITIES for x in v),
                             required=False)
            if t.get("available_dims") is not None:
                _expect_list_of_str(t, "available_dims", required=False)
            if t.get("joinable") is not None:
                _expect_type(t, "joinable", "布尔值", lambda v: isinstance(v, bool), required=False)

    # versions: list[{version, status, effective_from?, effective_to?, definition?}]
    if "versions" in entry and entry["versions"] is not None:
        _expect_type(entry, "versions", "版本列表", lambda v: isinstance(v, list), required=False)
        for i, ver in enumerate(entry["versions"]):
            if not isinstance(ver, dict):
                raise EntryValidationError(f"字段 versions[{i}] 格式错误：期望字典，收到 {_type_name(ver)}")
            if not isinstance(ver.get("version"), str) or not ver["version"].strip():
                raise EntryValidationError(f"字段 versions[{i}].version 缺失或为空（必填）")
            if ver.get("status") is not None and ver["status"] not in STATUSES:
                raise EntryValidationError(f"字段 versions[{i}].status 取值非法：期望 {sorted(STATUSES)} 之一")


def _validate_relation(entry: dict, mode: str = "create") -> None:
    for f in ("source", "target", "join_key"):
        _expect_type(entry, f, "字符串", lambda v: isinstance(v, str) and v.strip(),
                     required=(mode == "create"))
    _expect_type(entry, "type", "字符串", lambda v: isinstance(v, str), required=False)
    if entry.get("cardinality") is not None:
        if entry["cardinality"] not in CARDINALITIES:
            raise EntryValidationError(
                f"字段 cardinality 取值非法：期望 {sorted(CARDINALITIES)} 之一，收到 {entry['cardinality']!r}")
    if entry.get("description") is not None:
        _expect_type(entry, "description", "字符串", lambda v: isinstance(v, str), required=False)


def _validate_glossary(entry: dict, mode: str = "create") -> None:
    for f in ("term", "canonical"):
        _expect_type(entry, f, "字符串", lambda v: isinstance(v, str) and v.strip(),
                     required=(mode == "create"))
    _expect_enum(entry, "type", GLOSSARY_TYPES, required=(mode == "create"))


def _validate_config(entry: dict, mode: str = "create") -> None:
    if "key" not in entry or not isinstance(entry["key"], str) or not entry["key"].strip():
        raise EntryValidationError("字段 key 缺失或为空（必填）")


VALIDATORS = {
    "object": _validate_object,
    "function": _validate_function,
    "relation": _validate_relation,
    "glossary": _validate_glossary,
    "config": _validate_config,
}


def validate_entry(kind: str, entry: dict, mode: str = "create") -> None:
    """按 kind 校验 entry；失败抛 EntryValidationError（字段级 message）。

    mode: create（新建，全量必填）| update（合并更新，只校验传入字段，
    省略字段保留原值——与 Supabase upsert merge 语义一致）。
    调用方（ontology_register）在校验失败时返回
    {"error": f"字段级错误: {e}"} 而非裸 400。
    """
    validator = VALIDATORS.get(kind)
    if validator is None:
        raise EntryValidationError(
            f"不支持的注册类型: {kind}（支持 object/function/relation/glossary/config）")
    if not isinstance(entry, dict):
        raise EntryValidationError(f"entry 必须是字典，收到 {_type_name(entry)}")
    validator(entry, mode)
