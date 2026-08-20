"""探索性取数（P0/P1）：找表 → 写 SQL → 受控执行 → 观测。

背景：日常大量「指标未注册」的探索性取数（找数据源表→写 SQL→观测分析，
稳定后再固化注册）。engine 的安全模型（query_token 绑定翻译 SQL）有意挡死
裸 SQL 执行——探索通道在**同等只读护栏**下开放，但与正式取数链物理隔离。

安全边界（探索通道不可破坏）：
  * 只读：复用 Executor 的 FORBIDDEN 正则 + SQLite mode=ro（禁写禁 DDL）
  * 表名白名单：SQL 中出现的物理表名必须 ∈ 候选表（metadata_search/traverse
    返回值）——防 LLM 编造表名
  * 强制分区过滤：SELECT 必须带 dt 条件（防全表扫描；真实接入由引擎确保）
  * 行数上限：MAX_ROWS=1000（Executor 已保证）
  * 独立探索令牌：绑定 SQL 指纹 + 短 TTL，与 query_token 链隔离

前端交互（DSH 侧）：用户可选 NL / 纯 SQL / 混合三模式：
  * NL —— 自然语言走 OAG/MQL 正式链（指标注册后）
  * 纯 SQL —— Monaco（dt-sql-parser 高亮/补全）写 SQL → explore_validate
    → explore_execute → 结果回前端
  * 混合 —— NL 起草 SQL 骨架（LLM）→ Monaco 人工精修 → 同上
"""
from __future__ import annotations

import re

from .ontology_loader import Ontology

# 物理表名前缀（与 mql_schema.PHYSICAL_RE 一致，用于提取候选表）
TABLE_PREFIX_RE = re.compile(r"\b(?:dwd|dws|ads|dim|ods)_[a-z0-9_]+", re.I)
# dt 分区条件（探索 SQL 必须携带；真实数仓由引擎强制）
DT_FILTER_RE = re.compile(r"\bdt\s*(=|<|>|<=|>=|between|in)", re.I)


class ExploreValidationError(Exception):
    """探索 SQL 校验失败（字段级/安全级信息，可直接返回）。"""


def candidate_tables(onto: Ontology) -> set[str]:
    """从 Ontology 提取全部已注册物理表名（探索 SQL 的表名白名单）。"""
    tables: set[str] = set()
    for o in onto.objects.values():
        for t in o.get("source_tables", []):
            tables.add(t["table"])
    return tables


def extract_tables(sql: str) -> list[str]:
    """从 SQL 提取出现的物理表名（去重保序）。"""
    seen, out = set(), []
    for m in TABLE_PREFIX_RE.findall(sql):
        name = m.lower()
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def validate_explore_sql(sql: str, allowed_tables: set[str]) -> dict:
    """探索 SQL 校验：只读 + 表名白名单 + 强制分区。

    返回 {ok, errors[], tables[]}；errors 非空时调用方拒绝执行。
    """
    errors: list[str] = []
    if not isinstance(sql, str) or not sql.strip():
        return {"ok": False, "errors": ["SQL 不能为空"], "tables": []}

    # 1. 只读：禁止写语句/DDL（与 Executor.FORBIDDEN 一致，双保险）
    from .executor import FORBIDDEN
    if FORBIDDEN.search(sql):
        errors.append("探索 SQL 只允许 SELECT/CTE 查询，禁止写操作/DDL")
        return {"ok": False, "errors": errors, "tables": []}

    # 2. 必须以 SELECT 开头（CTE 允许 WITH 开头）
    head = sql.lstrip().lower()
    if not (head.startswith("select") or head.startswith("with")):
        errors.append("探索 SQL 必须以 SELECT 或 WITH(CTE) 开头")

    # 3. 表名白名单：SQL 中每个物理表名必须 ∈ 候选表
    tables = extract_tables(sql)
    unknown = [t for t in tables if t not in allowed_tables]
    if unknown:
        errors.append(f"SQL 引用未注册表: {unknown}（只能使用 metadata_search/ontology_search "
                      "返回的候选表）")
    if not tables:
        errors.append("SQL 未引用任何已注册物理表（无法执行）")

    # 4. 强制分区过滤（防全表扫描）
    if tables and not DT_FILTER_RE.search(sql):
        errors.append("探索 SQL 必须带分区条件（dt = ... / dt BETWEEN ... / dt IN ...）"
                      "——防全表扫描")

    return {"ok": not errors, "errors": errors, "tables": tables}
