"""MCP Server：把 Data Agent 确定性引擎暴露给 DSH（dsh-mcp-client 以 stdio 拉起）。

工具清单（保持精简，schema 常驻上下文有 token 成本）：
  1. ontology_search     —— OAG Step1/2：按名称/别名查对象、指标、属性
  2. ontology_traverse   —— OAG Step3：BFS 关系图扩展（join_key / required_filter / 公式）
  3. mql_validate        —— MQL 校验（安全边界：物理渗入检测）
  4. mql_explain         —— 把 MQL 转成人类可读的确认信息（用户在翻译执行前确认）
  5. semantic_translate  —— 确定性翻译：MQL → 可执行 SQL（含权限注入/表选择/默认 t-1）
  6. execute_sql         —— 只读执行翻译出的 SQL，返回结果集
  7. ddl_generate        —— 路径 D：从 Ontology 对象生成建表 DDL（遵守分层命名规范）
  8. scheduler_submit    —— 路径 D：提交调度任务（预留：待接入平台 mcp-scheduler）

环境变量：
  DATA_AGENT_BACKEND —— backend 目录（默认取本文件上级的上级）
  DATA_AGENT_DB      —— SQLite 库路径（默认 backend/seed/sample.db）
"""
from __future__ import annotations

import os
import sys
import warnings
from pathlib import Path

# mcp 1.x 依赖 pydantic-settings 的已知无害告警，启动时静默
warnings.filterwarnings("ignore", message=".*incomplete definition.*")

# 无论以何种方式启动（uv run / 直接 python），都保证能 import core
BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from core import Executor, MqlValidator, Ontology, Translator  # noqa: E402

BASE = Path(os.environ.get("DATA_AGENT_BACKEND", BACKEND_ROOT))
DB = Path(os.environ.get("DATA_AGENT_DB", BASE / "seed" / "sample.db"))

_onto = Ontology(BASE / "ontology")
_validator = MqlValidator(_onto)
_translator = Translator(_onto)
_executor = Executor(str(DB))

mcp = FastMCP("data-agent")


@mcp.tool()
def ontology_search(query: str) -> str:
    """OAG Step1/2：按名称/别名检索业务对象、指标与属性。只返回 Ontology 中已注册的内容，不得发明。"""
    q = query.strip().lower()
    hits = []
    for name, o in _onto.objects.items():
        if q in name.lower() or q in o.get("display_name", "").lower():
            props = ", ".join(p["name"] for p in o.get("properties", []))
            hits.append(f"[对象] {name}（{o.get('display_name')}）：{o.get('description','')}；属性: {props}")
    for name, f in _onto.functions.items():
        if q in name.lower() or q in f.get("display_name", "").lower():
            hits.append(f"[指标] {name}（{f.get('display_name')}）：{f.get('description','')}；公式: {f['formula']}；版本: {f.get('version')}")
    for p in _onto.all_properties():
        if q in p.lower():
            hits.append(f"[属性] {p}（归属 {_onto.owner_of(p)}）")
    return "\n".join(hits) if hits else f"未命中：{query}（触发模糊匹配 + 澄清，不要编造）"


@mcp.tool()
def ontology_traverse(root: str, max_depth: int = 2) -> str:
    """OAG Step3：从业务对象出发沿关系图 BFS 扩展，返回 JOIN 键、必要过滤、可达对象。"""
    if root not in _onto.objects:
        return f"对象 {root} 不存在"
    lines = [f"[{root}] {_onto.objects[root].get('description','')}",
             f"  必要过滤: {_onto.objects[root].get('required_filters', [])}"]
    from collections import deque
    q = deque([(root, 0)])
    seen = {root}
    while q:
        node, depth = q.popleft()
        if depth >= max_depth:
            continue
        for (nxt, jk) in _onto.edges.get(node, []):
            if nxt in seen:
                continue
            seen.add(nxt)
            lines.append(f"  {'  ' * depth}→ {node} --{jk}--> {nxt}")
            q.append((nxt, depth + 1))
    return "\n".join(lines)


@mcp.tool()
def mql_validate(mql: dict) -> dict:
    """MQL 校验（安全边界）：schema + 物理渗入检测。任何物理表/字段名出现在 MQL 中都会被拒绝。"""
    return _validator.validate(mql)


@mcp.tool()
def semantic_translate(mql: dict, user: dict | None = None) -> dict:
    """确定性翻译引擎：MQL → 可执行 SQL。含行级权限注入（user.region_ids）、
    表选择（预聚合/明细+多表 JOIN）、required_filter、默认时间 t-1。零 LLM。"""
    result = _validator.validate(mql)
    if not result["ok"]:
        return {"error": "MQL 校验失败", "validation": result}
    return _translator.translate(mql, user or {}, dialect="sqlite")


@mcp.tool()
def execute_sql(sql: str) -> dict:
    """只读执行 SQL（仅 SELECT/CTE，禁写；行数上限 1000），返回结果集。"""
    return _executor.execute(sql)


@mcp.tool()
def mql_explain(mql: dict) -> dict:
    """把 MQL 转成人类可读的确认信息。用户在 semantic_translate 执行【之前】用本工具
    把即将查询的指标口径/维度/过滤/时间展示给用户确认。"""
    v = _validator.validate(mql)
    if not v["ok"]:
        return {"ok": False, "validation": v}
    metrics = []
    for m in (mql.get("metrics") or [{"name": mql.get("metric")}]):
        name = m["name"] if isinstance(m, dict) else m
        fn = _onto.get_function(name)
        metrics.append({"name": name, "display_name": fn["display_name"],
                        "formula": fn["formula"], "version": fn.get("version"),
                        "owner": fn["owner"]})
    def prop_display(p):
        o = _onto.owner_of(p)
        if o:
            for pp in _onto.objects[o].get("properties", []):
                if pp["name"] == p:
                    return pp.get("description") or p
        return p
    dims = [{"name": d["name"], "meaning": prop_display(d["name"]),
             "granularity": d.get("granularity")}
            for d in mql.get("dimensions", [])]
    filters = [{"field": f["field"], "meaning": prop_display(f["field"]),
                "operator": f["operator"], "value": f["value"]}
               for f in mql.get("filters", [])]
    tr = mql.get("time_range")
    time_desc = (str(tr) if tr else "t-1（昨天，默认）")
    return {"ok": True, "metrics": metrics, "dimensions": dims, "filters": filters,
            "time_range": time_desc,
            "time_defaulted": tr is None,
            "confirm_required": True}


@mcp.tool()
def ddl_generate(obj_name: str, layer: str, domain: str = "ord",
                 subject: str = None) -> dict:
    """路径 D（ETL）：从 Ontology 对象生成建表 DDL 草稿，严格遵守数仓分层命名规范：
    表名 = {layer}_{domain}_{subject}_{粒度后缀}（后缀：明细 _di、日汇总 _1d、月汇总 _1m）。
    口径/类型以 Ontology 为准，生成后需人工 Review + 走审批。"""
    o = _onto.get_object(obj_name)
    if not o:
        return {"error": f"对象 {obj_name} 不存在"}
    subject = subject or obj_name.lower()
    suffix = {"DWD": "_di", "DWS": "_1d", "ADS": "_1d", "DIM": ""}.get(layer.upper(), "_di")
    table = f"{layer.lower()}_{domain}_{subject}{suffix}"
    type_map = {"string": "VARCHAR(255)", "decimal": "DECIMAL(18,2)", "date": "DATE",
                "int": "BIGINT", "bigint": "BIGINT", "integer": "BIGINT"}
    cols = []
    for p in o.get("properties", []):
        cols.append(f"  {p['name']} {type_map.get(p.get('type'), 'STRING')} "
                    f"COMMENT '{p.get('description', p['name'])}'")
    for c in o.get("required_filters", []):
        field = c.split(" ")[0]
        if field not in {p["name"] for p in o.get("properties", [])}:
            cols.append(f"  {field} BIGINT COMMENT '必要过滤标志 {c}'")
    # 非 DIM 层统一加分区 dt + 主键
    if layer.upper() != "DIM":
        cols.append(f"  {_onto.partition_col} VARCHAR(8) NOT NULL COMMENT '分区 {_onto.partition_col}(yyyyMMdd)'")
    ddl = (f"CREATE TABLE IF NOT EXISTS {table} (\n" + ",\n".join(cols) +
           "\n) COMMENT '" + o.get("description", "") + "'\n" +
           f"PARTITION BY RANGE({_onto.partition_col})();\n")
    return {"table": table, "layer": layer.upper(), "domain": domain,
            "subject": subject, "ddl": ddl, "review_required": True}


@mcp.tool()
def scheduler_submit(task_spec: dict) -> dict:
    """路径 D（ETL）：提交调度任务（**预留**）。当前未接入公司调度平台 MCP（mcp-scheduler），
    接入后在此补充核心逻辑：任务依赖、调度周期（cron）、告警通道、幂等键。"""
    return {"error": "调度配置未接入平台 MCP（预留）。"
                     "接入 mcp-scheduler 后：提交任务依赖/周期/告警，带幂等键。"}


if __name__ == "__main__":
    mcp.run(transport="stdio")
