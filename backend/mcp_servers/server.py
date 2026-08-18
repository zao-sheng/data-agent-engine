"""MCP Server：把 Data Agent 确定性引擎暴露给 DSH（dsh-mcp-client 以 stdio 拉起）。

工具清单（保持精简，schema 常驻上下文有 token 成本）：
  1. ontology_search     —— OAG Step1/2：按名称/别名查对象、指标、属性
  2. ontology_traverse   —— OAG Step3：BFS 关系图扩展（join_key / required_filter / 公式）
  3. mql_validate        —— MQL 校验（安全边界：物理渗入检测）
  4. semantic_translate  —— 确定性翻译：MQL → 可执行 SQL（含权限注入/表选择/默认 t-1）
  5. execute_sql         —— 只读执行翻译出的 SQL，返回结果集

环境变量：
  DATA_AGENT_BACKEND —— backend 目录（默认取本文件上级的上级）
  DATA_AGENT_DB      —— SQLite 库路径（默认 backend/seed/sample.db）
"""
from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from core import Executor, MqlValidator, Ontology, Translator

BASE = Path(os.environ.get("DATA_AGENT_BACKEND", Path(__file__).resolve().parent.parent))
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


if __name__ == "__main__":
    mcp.run(transport="stdio")
