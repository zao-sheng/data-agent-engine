"""Spark SQL DDL 生成器（路径 D：建表草稿）。

企业落地约定：目标引擎为 Spark（Hive 风格 SQL），演示/交付物统一 Spark SQL：
  * 类型映射：STRING / DECIMAL(p,s) / DATE / INT / BIGINT
  * 分区：事实表（DWD/DWS/ADS）统一 `PARTITIONED BY (dt STRING)`，分区列不进普通列
  * 维表（DIM）无分区
  * 列级/表级 COMMENT 保留口径说明（口径可追溯）

命名规范（与 warehouse-standards skill 一致）：
  {layer}_{domain}_{subject}_{粒度后缀}：明细 _di / 日汇总 _1d / 月汇总 _1m / 维表无后缀
"""
from __future__ import annotations

from .ontology_loader import Ontology

# Spark SQL 类型映射（Ontology 属性 type → Spark 类型）
SPARK_TYPE_MAP = {
    "string": "STRING",
    "decimal": "DECIMAL(18,2)",
    "date": "DATE",
    "int": "INT",
    "integer": "INT",
    "bigint": "BIGINT",
}

# 粒度后缀（非 DIM 层）
LAYER_SUFFIX = {"DWD": "_di", "DWS": "_1d", "ADS": "_1d", "DIM": ""}


def spark_type(t: str | None) -> str:
    return SPARK_TYPE_MAP.get((t or "string").lower(), "STRING")


def table_name(layer: str, domain: str, subject: str) -> str:
    """按命名规范生成表名。"""
    layer = layer.upper()
    suffix = LAYER_SUFFIX.get(layer, "_di")
    return f"{layer.lower()}_{domain}_{subject}{suffix}"


def generate_ddl(onto: Ontology, obj_name: str, layer: str, domain: str | None = None,
                 subject: str | None = None) -> dict:
    """从 Ontology 对象生成 Spark SQL 建表草稿。

    返回 {table, layer, domain, subject, ddl, review_required}；
    生成后需人工 Review + 审批（路径 D 阶段 3）。
    domain 缺省取对象自身的业务域（o.domain，如 ord/prd/usr）——
    与本体治理字段对齐，不再硬编码 ord。
    """
    o = onto.get_object(obj_name)
    if not o:
        return {"error": f"对象 {obj_name} 不存在"}
    layer = layer.upper()
    domain = domain or o.get("domain") or "ord"
    subject = subject or obj_name.lower()
    table = table_name(layer, domain, subject)

    lines: list[str] = []
    for p in o.get("properties", []):
        lines.append(f"  {p['name']} {spark_type(p.get('type'))} "
                     f"COMMENT '{p.get('description', p['name'])}'")
    # required_filter 标志字段（如 is_valid）若不在属性里则补充
    for c in o.get("required_filters", []):
        field = c.split(" ")[0]
        if field not in {p["name"] for p in o.get("properties", [])}:
            lines.append(f"  {field} INT COMMENT '必要过滤标志 {c}'")

    cols = ",\n".join(lines)
    table_comment = o.get("description", "")
    if layer == "DIM":
        # 维表无分区
        ddl = (f"-- Spark SQL DDL（目标引擎 Spark，Hive 风格）\n"
               f"CREATE TABLE IF NOT EXISTS {table} (\n{cols}\n"
               f") COMMENT '{table_comment}'\n"
               f"STORED AS PARQUET;\n")
    else:
        # 事实表统一 dt 分区（yyyyMMdd），分区列不进普通列
        ddl = (f"-- Spark SQL DDL（目标引擎 Spark，Hive 风格）\n"
               f"CREATE TABLE IF NOT EXISTS {table} (\n{cols}\n"
               f") COMMENT '{table_comment}'\n"
               f"PARTITIONED BY ({onto.partition_col} STRING COMMENT '分区 {onto.partition_col}(yyyyMMdd)')\n"
               f"STORED AS PARQUET;\n")

    return {"table": table, "layer": layer, "domain": domain,
            "subject": subject, "engine": "spark", "ddl": ddl,
            "review_required": True}
