"""MCP Server：把 Data Agent 确定性引擎暴露给 DSH（dsh-mcp-client 以 stdio 拉起）。

工具清单（保持精简，schema 常驻上下文有 token 成本）：
  1. ontology_search     —— OAG Step1/2：按名称/别名查对象、指标、属性
  2. ontology_traverse   —— OAG Step3：BFS 关系图扩展（join_key / required_filter / 公式）
  3. mql_validate        —— MQL 校验（安全边界：物理渗入检测）
  4. mql_explain         —— 把 MQL 转成人类可读的确认信息（用户在翻译执行前确认）
  5. semantic_translate  —— 确定性翻译：MQL → 可执行 SQL（含权限注入/表选择/默认 t-1）
                          —— 翻译成功即签发 query_token（绑定 SQL）
  6. execute_sql         —— 只读执行翻译出的 SQL（必须携带 query_token，防绕过翻译直查）
  7. ddl_generate        —— 路径 D：从 Ontology 对象生成建表 DDL（遵守分层命名规范）
  8. scheduler_submit    —— 路径 D：提交调度任务（预留：待接入平台 mcp-scheduler）

安全模型（P0）：
  * 查询令牌：semantic_translate 签发、execute_sql 校验，SQL 与令牌强绑定，
    任何绕过翻译引擎的裸 SQL 都会被拒绝（行级权限/表选择/required_filter
    不再可被绕过）。
  * 审计日志：所有工具调用落 jsonl（backend/logs/audit.jsonl，轮转清理），
    记录调用方、动作、入参摘要、耗时与结果规模。

环境变量（统一 DATA_AGENT_ 前缀，见 core/config.py）：
  DATA_AGENT_BACKEND   —— backend 目录（默认取本文件上级的上级）
  DATA_AGENT_DB        —— SQLite 库路径（默认 backend/seed/sample.db）
  DATA_AGENT_LOG_DIR   —— 日志目录（默认 backend/logs）
  DATA_AGENT_TOKEN_TTL —— 查询令牌有效期秒（默认 300）
"""
from __future__ import annotations

import os
import sys
import time
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
from core.audit import audit, setup_audit_logger  # noqa: E402
from core.config import CONFIG  # noqa: E402
from core.confirm_token import ConfirmTokenStore  # noqa: E402
from core.ddl_gen import generate_ddl  # noqa: E402
from core.etl_gen import generate_etl  # noqa: E402
from core.query_token import QueryTokenStore  # noqa: E402
from core.runtime_log import log_error, log_info, log_warn, setup_runtime_logger  # noqa: E402
from core.startup_check import run_startup_checks, startup_status  # noqa: E402

BASE = Path(os.environ.get("DATA_AGENT_BACKEND", BACKEND_ROOT))
DB = Path(os.environ.get("DATA_AGENT_DB", BASE / "seed" / "sample.db"))

# 运行日志（P2）：jsonl + 轮转（启动后立刻可用）
setup_runtime_logger(CONFIG.log_dir, level=CONFIG.log_level,
                     max_bytes=CONFIG.audit_max_bytes,
                     backup_count=CONFIG.audit_backup_count)

_onto = Ontology(BASE / "ontology")
_validator = MqlValidator(_onto)
_translator = Translator(_onto)
_executor = Executor(str(DB))

# 启动自检（P2）：fail-fast，启动即暴露配置/介质问题
_STARTUP_CHECKS = run_startup_checks(_onto, _executor)
for _c in _STARTUP_CHECKS:
    if _c.ok:
        log_info("startup_check_ok", name=_c.name, detail=_c.detail)
    else:
        log_error("startup_check_failed", name=_c.name, detail=_c.detail,
                  required=_c.required)
if not all(_c.ok for _c in _STARTUP_CHECKS if _c.required):
    log_warn("startup_checks_partial_failure",
             failed=[_c.name for _c in _STARTUP_CHECKS if not _c.ok])

# 查询令牌存储（P0-1）：翻译签发 → 执行校验
_token_store = QueryTokenStore(ttl_seconds=CONFIG.query_token_ttl,
                               max_tokens=CONFIG.query_token_max)

# 确认令牌存储（P4-14）：mql_explain 签发 → semantic_translate 强制携带
_confirm_store = ConfirmTokenStore(ttl_seconds=CONFIG.confirm_token_ttl,
                                   max_tokens=CONFIG.confirm_token_max)

# 审计日志（P0-3）：jsonl + 轮转
setup_audit_logger(CONFIG.log_dir, max_bytes=CONFIG.audit_max_bytes,
                   backup_count=CONFIG.audit_backup_count)

mcp = FastMCP("data-agent")


def _audit_tool(fn):
    """工具调用审计包装：记录耗时/成败/入参摘要。

    functools.wraps 保留签名（FastMCP 依赖 inspect.signature 生成 schema）。
    审计失败不影响主流程。
    """
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        t0 = time.time()
        try:
            result = fn(*args, **kwargs)
            outcome = "ok" if not (isinstance(result, dict) and result.get("error")) else "error"
            audit(fn.__name__, "call", outcome,
                  elapsed_ms=int((time.time() - t0) * 1000),
                  args=_summarize_args(kwargs or {}, fn.__name__),
                  result=_summarize_result(result, fn.__name__))
            return result
        except Exception as e:  # noqa: BLE001 —— 审计后重新抛出，由 MCP 层转为错误结果
            audit(fn.__name__, "call", "exception",
                  elapsed_ms=int((time.time() - t0) * 1000),
                  args=_summarize_args(kwargs or {}, fn.__name__),
                  error=str(e)[:500])
            raise
    return wrapper


@mcp.tool()
@_audit_tool
def health_check() -> dict:
    """健康检查：返回引擎状态（ontology 规模、数据介质、自检结果）。排障入口。"""
    return startup_status(_onto, _executor)


def _summarize_args(kwargs: dict, tool: str) -> dict:
    """入参摘要：大对象只保留关键字段，避免审计日志膨胀。"""
    out: dict = {}
    for k, v in kwargs.items():
        if k == "mql" and isinstance(v, dict):
            out["mql"] = {
                "metrics": [m.get("name") if isinstance(m, dict) else m
                            for m in (v.get("metrics") or [])],
                "dimensions": [d.get("name") for d in v.get("dimensions", [])],
                "filters": [f.get("field") for f in v.get("filters", [])],
                "time_range": v.get("time_range"),
            }
        elif k == "sql":
            out["sql_fp"] = QueryTokenStore.fingerprint(str(v))
            out["sql_len"] = len(str(v))
        elif k == "user":
            out["user_region_ids"] = v.get("region_ids") if isinstance(v, dict) else None
        elif k == "query_token":
            out["query_token"] = f"{str(v)[:8]}…" if v else None
        elif isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        else:
            out[k] = type(v).__name__
    return out


def _summarize_result(result, tool: str) -> dict:
    """结果摘要：结果集只记录规模，不记录明细数据（隐私 + 体积）。"""
    if not isinstance(result, dict):
        return {"type": type(result).__name__}
    out: dict = {}
    if "rows" in result:
        out["row_count"] = result.get("row_count")
        out["truncated"] = result.get("truncated")
        out["columns"] = result.get("columns")
    elif "sql" in result:
        out["sql_fp"] = QueryTokenStore.fingerprint(str(result.get("sql", "")))
        out["dialect"] = result.get("metadata", {}).get("dialect")
        out["fact_table"] = result.get("metadata", {}).get("fact_table")
        out["tables_used"] = result.get("metadata", {}).get("tables_used")
    elif tool == "ontology_search":
        out["hits"] = len(str(result).splitlines()) if isinstance(result, str) else None
    return out


@mcp.tool()
@_audit_tool
def ontology_search(query: str) -> str:
    """OAG Step1/2：按名称/别名检索业务对象、指标与属性。只返回 Ontology 中已注册的内容，不得发明。"""
    hits = _onto.search(query)
    return "\n".join(hits) if hits else f"未命中：{query}（触发模糊匹配 + 澄清，不要编造）"


@mcp.tool()
@_audit_tool
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
@_audit_tool
def mql_validate(mql: dict) -> dict:
    """MQL 校验（安全边界）：schema + 物理渗入检测。任何物理表/字段名出现在 MQL 中都会被拒绝。"""
    return _validator.validate(mql)


@mcp.tool()
@_audit_tool
def semantic_translate(mql: dict, confirm_token: str,
                       user: dict | None = None, dialect: str = "sqlite") -> dict:
    """确定性翻译引擎：MQL → 可执行 SQL。含行级权限注入（user.region_ids）、
    表选择（预聚合/明细+多表 JOIN）、required_filter、默认时间 t-1。零 LLM。

    必须携带 mql_explain 返回的 confirm_token（绑定 MQL 指纹）——确认过的
    查询才能翻译，防跳过用户确认。翻译成功返回 query_token（绑定 SQL），
    execute_sql 必须携带——禁止绕过翻译引擎执行裸 SQL。
    dialect: sqlite（默认，已实现已测试）/ doris / mysql / hive / sparksql
    （远程方言仅翻译，需在 backend/.env 配置 DATA_AGENT_DSN_<方言> 并接入
    _execute_remote 后执行）。"""
    ok, reason = _confirm_store.verify(confirm_token, mql)
    if not ok:
        return {"error": f"确认令牌校验失败: {reason}"}
    result = _validator.validate(mql)
    if not result["ok"]:
        return {"error": "MQL 校验失败", "validation": result}
    tr = _translator.translate(mql, user or {}, dialect=dialect)
    if "error" in tr:
        return tr
    tr["query_token"] = _token_store.issue(tr["sql"])
    tr["confirmed"] = True
    return tr


@mcp.tool()
@_audit_tool
def execute_sql(sql: str, query_token: str) -> dict:
    """只读执行 SQL（仅 SELECT/CTE，禁写；行数上限 1000），返回结果集。

    必须携带 semantic_translate 返回的 query_token：令牌与 SQL 强绑定，
    禁止绕过翻译引擎执行任意 SQL（行级权限/表选择/口径过滤由翻译环节保证）。"""
    ok, reason = _token_store.verify(query_token, sql)
    if not ok:
        return {"error": reason}
    return _executor.execute(sql)


@mcp.tool()
@_audit_tool
def mql_explain(mql: dict) -> dict:
    """把 MQL 转成人类可读的确认信息，并签发 confirm_token。

    用户在 semantic_translate 执行【之前】必须先用本工具展示口径/维度/过滤/
    时间并请用户确认；返回的 confirm_token 是 semantic_translate 的必填参数
    （绑定当前 MQL 指纹，MQL 变更需重新 explain）——防止跳过确认直接翻译执行。"""
    v = _validator.validate(mql)
    if not v["ok"]:
        return {"ok": False, "validation": v}
    metrics = []
    for m in (mql.get("metrics") or [{"name": mql.get("metric")}]):
        name = m["name"] if isinstance(m, dict) else m
        fn = _onto.get_function(name)
        metrics.append({"name": name, "display_name": fn["display_name"],
                        "formula": fn["formula"], "version": fn.get("version"),
                        "owner": fn["owner"],
                        "family": fn.get("family"), "variant_label": fn.get("variant_label")})
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
            "confirm_required": True,
            "confirm_token": _confirm_store.issue(mql)}


@mcp.tool()
@_audit_tool
def metric_disambiguate(query: str) -> dict:
    """指标歧义识别（多口径指标族）：精确命中 → 返回 exact；
    命中指标族（如 GMV 有支付/下单/消费口径）→ 返回 family 全部变体及差异，供用户确认；
    未命中 → 返回候选。"""
    q = query.strip().lower()
    exact = None
    # ① 黑话命中（glossary 中 type=metric 的条目，如"成交额"→gmv）
    ti = _onto.term_index.get(q)
    if ti and ti["type"] == "metric":
        exact = _onto.functions.get(ti["canonical"])
    # ② 口径词优先（"支付GMV" → 支付口径变体）
    if exact is None:
        for f in _onto.functions.values():
            vl = f.get("variant_label", "")
            if vl and vl.replace("口径", "") in q:
                exact = f
                break
    # ③ 精确指标名
    if exact is None and q in _onto.functions:
        exact = _onto.functions[q]
    # ④ 族匹配
    family = None
    for fam, entry in _onto.families.items():
        if fam in q or q in fam:
            variants = []
            for v in entry["variants"]:
                f = _onto.functions[v]
                variants.append({"name": v, "display_name": f["display_name"],
                                 "formula": f["formula"],
                                 "required_filters": f.get("required_filters", []),
                                 "do_not": f.get("do_not", ""),
                                 "is_default": v == entry["default"]})
            family = {"name": fam, "default": entry["default"], "variants": variants}
            break

    if exact and family:
        status = "both"
    elif exact:
        status = "exact"
    elif family:
        status = "family"
    else:
        status = "none"

    out = {"status": status, "query": query}
    if exact:
        out["exact"] = {"name": exact["name"], "display_name": exact["display_name"],
                        "formula": exact["formula"], "version": exact.get("version"),
                        "variant_label": exact.get("variant_label"),
                        "family": exact.get("family")}
    if family:
        out["family"] = family
    if status == "none":
        out["candidates"] = sorted(_onto.functions)
    return out


@mcp.tool()
@_audit_tool
def term_normalize(text: str) -> dict:
    """业务黑话/别名归一（OAG Step 0）：把用户问题中的黑话/别名
    （如 poi/店铺→Store、goods/商品→Product）确定性地归一为 Ontology 标准术语。
    返回 {normalized_text, mappings:[{raw, canonical, type}]}；回答时应按 mappings 回译用户用词。"""
    norm, mappings = _onto.normalize_terms(text)
    return {"normalized_text": norm, "mappings": mappings, "original": text}


@mcp.tool()
@_audit_tool
def ddl_generate(obj_name: str, layer: str, domain: str = "ord",
                 subject: str = None) -> dict:
    """路径 D（ETL）：从 Ontology 对象生成建表 DDL 草稿（Spark SQL，Hive 风格）。
    严格遵守数仓分层命名规范：表名 = {layer}_{domain}_{subject}_{粒度后缀}
    （后缀：明细 _di、日汇总 _1d、月汇总 _1m）；事实表统一 dt 分区。
    口径/类型以 Ontology 为准，生成后需人工 Review + 走审批。"""
    return generate_ddl(_onto, obj_name, layer, domain=domain, subject=subject)


@mcp.tool()
@_audit_tool
def etl_generate(obj_name: str, layer: str, domain: str = "ord",
                 subject: str = None,
                 metrics: list[str] = None,
                 dimensions: list[str] = None) -> dict:
    """路径 D（ETL）：从 Ontology 生成 Spark SQL ETL 管道草稿
    （INSERT OVERWRITE 聚合，源=DWD 明细 → 目标=汇总/应用表）。
    指标公式/required_filter/维度映射以 Ontology 为准，跨域指标自动 CTE 合并
    （与翻译引擎多指标策略一致）。生成后需人工 Review + 走审批。"""
    return generate_etl(_onto, obj_name, layer, domain=domain, subject=subject,
                        metrics=metrics, dimensions=dimensions)


@mcp.tool()
@_audit_tool
def scheduler_submit(task_spec: dict) -> dict:
    """路径 D（ETL）：提交调度任务（**预留**）。当前未接入公司调度平台 MCP（mcp-scheduler），
    接入后在此补充核心逻辑：任务依赖、调度周期（cron）、告警通道、幂等键。"""
    return {"error": "调度配置未接入平台 MCP（预留）。"
                     "接入 mcp-scheduler 后：提交任务依赖/周期/告警，带幂等键。"}


if __name__ == "__main__":
    mcp.run(transport="stdio")
