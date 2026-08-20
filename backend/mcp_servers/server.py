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
import re
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
from core.entry_validator import EntryValidationError, validate_entry  # noqa: E402
from core.explore import (
    candidate_tables, extract_tables, validate_explore_sql)  # noqa: E402
from core.explore_promote import build_register_draft  # noqa: E402
from core.intent import classify_intent  # noqa: E402
from core.metadata import MetadataService  # noqa: E402
from core.modeling_plan import generate_modeling_plan  # noqa: E402
from core.ontology_store import TableMetadataStore, create_store  # noqa: E402
from core.table_metadata import collect_metadata_from_entry  # noqa: E402
from core.ontology_writer import create_writer, writer_supports  # noqa: E402
from core.query_token import QueryTokenStore  # noqa: E402
from core.runtime_log import log_error, log_info, log_warn, setup_runtime_logger  # noqa: E402
from core.startup_check import run_startup_checks, startup_status  # noqa: E402

BASE = Path(os.environ.get("DATA_AGENT_BACKEND", BACKEND_ROOT))
DB = Path(os.environ.get("DATA_AGENT_DB", BASE / "seed" / "sample.db"))

# 运行日志（P2）：jsonl + 轮转（启动后立刻可用）
setup_runtime_logger(CONFIG.log_dir, level=CONFIG.log_level,
                     max_bytes=CONFIG.audit_max_bytes,
                     backup_count=CONFIG.audit_backup_count)

# 本体存储（1A/1B/2）：默认 YAML（发布基线快照）；opt-in SQLite 编译产物
# （DATA_AGENT_ONTOLOGY_STORE=sqlite）或 Supabase 真源（=supabase，多人编辑）
_onto = Ontology(store=create_store(CONFIG.ontology_store,
                                    base=CONFIG.ontology_dir,
                                    db=CONFIG.ontology_db,
                                    url=CONFIG.supabase_url,
                                    key=CONFIG.supabase_key,
                                    schema=CONFIG.supabase_schema))
_validator = MqlValidator(_onto)
_translator = Translator(_onto)
_executor = Executor(str(DB))
# 元数据检索：Supabase 模式配 ontology_tables 查库（多人维护表元数据），
# yaml/sqlite 模式 tables_store=None 回落本地（Ontology + 内置血缘）
_tables_store = (TableMetadataStore(CONFIG.supabase_url, CONFIG.supabase_key,
                                    schema=CONFIG.supabase_schema)
                 if CONFIG.ontology_store == "supabase" and CONFIG.supabase_url
                 else None)
_metadata = MetadataService(_onto, db_path=str(DB), tables_store=_tables_store)

# 本体写入器（多人编辑闭环）：按 store 类型分发（yaml 文件 / supabase 表）
_writer = create_writer(CONFIG.ontology_store,
                        base=CONFIG.ontology_dir,
                        url=CONFIG.supabase_url,
                        key=CONFIG.supabase_key,
                        schema=CONFIG.supabase_schema)


def _reload_ontology() -> dict:
    """运行时重新加载本体（多人编辑后刷新当前会话）。

    重建 Ontology（从 store 重读）并刷新依赖它的 validator/translator/metadata。
    工具函数访问模块级 _onto，reload 后新值自动生效；_executor 不依赖本体，无需重建。
    """
    global _onto, _validator, _translator, _metadata, _tables_store
    store = create_store(CONFIG.ontology_store,
                         base=CONFIG.ontology_dir,
                         db=CONFIG.ontology_db,
                         url=CONFIG.supabase_url,
                         key=CONFIG.supabase_key,
                         schema=CONFIG.supabase_schema)
    _onto = Ontology(store=store)
    _validator = MqlValidator(_onto)
    _translator = Translator(_onto)
    _tables_store = (TableMetadataStore(CONFIG.supabase_url, CONFIG.supabase_key,
                                        schema=CONFIG.supabase_schema)
                     if CONFIG.ontology_store == "supabase" and CONFIG.supabase_url
                     else None)
    _metadata = MetadataService(_onto, db_path=str(DB), tables_store=_tables_store)
    return {
        "ok": True,
        "store": CONFIG.ontology_store,
        "objects": len(_onto.objects),
        "functions": len(_onto.functions),
        "relations": len(_onto.relations),
        "glossary": len(_onto.term_index),
        "note": "本体已重新加载；当前会话后续查询使用最新本体",
    }

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

# 探索令牌存储（P0/P1）：explore_validate 签发 → explore_execute 校验
# 独立于 query_token 链（探索 SQL 未走翻译引擎），但同样绑定 SQL 指纹
_explore_store = QueryTokenStore(ttl_seconds=120, max_tokens=100)

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
def ontology_search(query: str, domain: str | None = None) -> str:
    """OAG Step1/2：按名称/别名检索业务对象、指标与属性。只返回 Ontology 中已注册的内容，不得发明。

    可选 domain 参数：按业务域过滤候选集（跨域查询按域路由，如 domain="ord" 只返回订单域）。
    返回行含类型/域/状态/标签，供 LLM 判断对象归属与路由。"""
    hits = _onto.search_by_domain(query, domain)
    return "\n".join(hits) if hits else f"未命中：{query}（触发模糊匹配 + 澄清，不要编造）"


@mcp.tool()
@_audit_tool
def ontology_traverse(root: str, max_depth: int = 2) -> str:
    """OAG Step3：从业务对象出发沿关系图 BFS 扩展，返回 JOIN 键、关系语义（类型/基数）、可达对象。"""
    if root not in _onto.objects:
        return f"对象 {root} 不存在"
    o = _onto.objects[root]
    lines = [f"[{root}] {o.get('description','')}",
             f"  类型: {o.get('object_type','fact')}；域: {o.get('domain','unknown')}；状态: {o.get('status','active')}",
             f"  必要过滤: {o.get('required_filters', [])}"]
    from collections import deque
    q = deque([(root, 0)])
    seen = {root}
    while q:
        node, depth = q.popleft()
        if depth >= max_depth:
            continue
        for rel in _onto.relation_meta.get(node, []):
            nxt = rel["target"]
            if nxt in seen:
                continue
            seen.add(nxt)
            sem = f"{rel['type']}({rel['cardinality']})" if rel.get("type") else "关联"
            lines.append(f"  {'  ' * depth}→ {node} --{rel['join_key']} [{sem}]--> {nxt}"
                         + (f"（{rel['description']}）" if rel.get("description") else ""))
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
                        "domain": fn.get("domain", "unknown"),
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


def _metric_dimension(fn: dict) -> str:
    """指标量纲：count（计数：公式含 COUNT）| amount（金额：SUM 且无 COUNT）| other。"""
    formula = fn.get("formula", "")
    if "COUNT" in formula.upper():
        return "count"
    if "SUM" in formula.upper():
        return "amount"
    return "other"


def _query_dimension(q: str) -> str | None:
    """从查询词推断用户要的量纲：订单/单/数量/多少单/笔/人数 → count；
    金额/多少钱/额/钱 → amount；无信号 → None。"""
    count_hint = ["订单", "单数", "多少单", "数量", "多少个", "笔数", "笔", "人数",
                  "用户数", "次数", "count", "量", "单"]
    amount_hint = ["金额", "多少钱", "多少元", "钱", "额", "金额数", "gmv", "客单价"]
    low = q.lower()
    for w in count_hint:
        if w in low:
            return "count"
    for w in amount_hint:
        if w in low:
            return "amount"
    return None


@mcp.tool()
@_audit_tool
def metric_disambiguate(query: str) -> dict:
    """指标歧义识别（多口径指标族）：精确命中 → 返回 exact；
    命中指标族（如 GMV 有支付/下单/消费口径）→ 返回 family 全部变体及差异，供用户确认；
    未命中 → 返回候选。

    **量纲感知**（防跨量纲误替）：查询含量纲信号（订单/单/数量 → 计数；
    金额/多少钱 → 金额）时，命中指标若量纲不符（如查"消费订单数"却命中
    金额指标 consume_gmv）→ 不返回 exact，降级到 candidates 并标注
    dimension_mismatch——**跨量纲（单数 vs 金额）绝不可作为替代候选**。
    """
    q = query.strip().lower()
    q_dim = _query_dimension(q)
    exact = None
    mismatch_reason: str | None = None

    # ① 黑话命中（glossary 中 type=metric 的条目，如"成交额"→gmv）
    ti = _onto.term_index.get(q)
    if ti and ti["type"] == "metric":
        cand = _onto.functions.get(ti["canonical"])
        if cand and q_dim and _metric_dimension(cand) != q_dim:
            mismatch_reason = (f"黑话 {q!r} 命中 {cand['name']}（{cand.get('display_name')}），"
                               f"量纲为 {_metric_dimension(cand)}，与查询的量纲 {q_dim} 不符——跨量纲不可替代")
        else:
            exact = cand
    # ② 口径词优先（"支付GMV" → 支付口径变体）
    if exact is None:
        for f in _onto.functions.values():
            vl = f.get("variant_label", "")
            if vl and vl.replace("口径", "") in q:
                if q_dim and _metric_dimension(f) != q_dim:
                    mismatch_reason = (f"口径词 {vl!r} 命中 {f['name']}（{f.get('display_name')}），"
                                       f"量纲为 {_metric_dimension(f)}，与查询的量纲 {q_dim} 不符——跨量纲不可替代")
                else:
                    exact = f
                break
    # ③ 精确指标名
    if exact is None and q in _onto.functions:
        f = _onto.functions[q]
        if q_dim and _metric_dimension(f) != q_dim:
            mismatch_reason = (f"指标名 {q!r} 命中 {f['name']}，量纲为 {_metric_dimension(f)}，"
                               f"与查询的量纲 {q_dim} 不符——跨量纲不可替代")
        else:
            exact = f
    # ④ 族匹配
    family = None
    for fam, entry in _onto.families.items():
        if fam in q or q in fam:
            variants = []
            for v in entry["variants"]:
                f = _onto.functions[v]
                variants.append({"name": v, "display_name": f["display_name"],
                                 "formula": f["formula"],
                                 "metric_type": _metric_dimension(f),
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

    out = {"status": status, "query": query,
           "query_dimension": q_dim}
    if exact:
        out["exact"] = {"name": exact["name"], "display_name": exact["display_name"],
                        "formula": exact["formula"], "version": exact.get("version"),
                        "variant_label": exact.get("variant_label"),
                        "family": exact.get("family"),
                        "metric_type": _metric_dimension(exact),
                        "domain": exact.get("domain", "unknown"),
                        "status": exact.get("status", "active"),
                        "owner": exact.get("owner", "")}
    if family:
        out["family"] = family
    if mismatch_reason:
        out["dimension_mismatch"] = mismatch_reason
        # 跨量纲命中不当作 exact → 候选给同量纲指标
        status = "none"
        out["status"] = "none"
    if status == "none" and not exact:
        # 候选：优先「同量纲 + 语义相关」（display_name/别名/名含查询 token），
        # 其次同量纲全部；查询无量纲信号时给全部
        if q_dim:
            def _rel(name: str, f: dict) -> bool:
                hay = (f.get("display_name", "") + name + f.get("variant_label", "")
                       + f.get("family", "")).lower()
                # 语义相关：查询中的中文子串（长度≥2）或英文 token 命中候选文本
                cn_parts = re.findall(r"[\u4e00-\u9fff]+", q)
                en_toks = re.findall(r"[a-z0-9_]+", q)
                for s in cn_parts:
                    for i in range(len(s) - 1):
                        if s[i:i+2] in hay:
                            return True
                return any(t in hay for t in en_toks if len(t) >= 2)
            same = [(n, f) for n, f in _onto.functions.items()
                    if _metric_dimension(f) == q_dim]
            related = [n for n, f in same if _rel(n, f)]
            out["candidates"] = related or [n for n, _ in same]
        else:
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
def intent_classify(text: str) -> dict:
    """意图识别（plan-routing Step1 的确定性预分类）：四类意图
    query(取数) / metadata(元数据咨询) / modeling(新建建模) / operation(其他操作) / unclear(低置信)。

    返回结构化证据：{intent, confidence(high/medium/low), path(A/metadata/D/direct/clarify),
    normalized_text, metric_hits, object_hits, evidence[], mixed[],
    metrics_missing(取数但指标未命中), note}。

    用法（LLM 侧）：
      * 证据充分（high/medium）→ 采信 intent 与 path；
      * mixed 非空 → 先执行主 intent，末尾询问另一诉求是否一并处理；
      * intent=unclear 或证据冲突 → 向用户澄清，禁止猜路径；
      * query 且 metrics_missing=true → 告知缺失 + 询问是否新建（走 D），禁止近似替代。"""
    return classify_intent(_onto, text)


@mcp.tool()
@_audit_tool
def metadata_search(query: str) -> dict:
    """元数据检索（元数据咨询意图）：查表/指标维度/口径/加工逻辑(血缘)/就绪时间。
    返回结构化结果：tables(表信息) / metrics(指标口径) / lineage(加工逻辑) /
    readiness(就绪时间)。当前基于 Ontology 与样例库实现；接入真实元数据
    平台后由接口替换，返回结构不变。"""
    try:
        return _metadata.search(query)
    except Exception as e:  # noqa: BLE001 —— 缺陷②：不裸抛，返回结构化错误
        return {"query": query, "tables": [], "metrics": [], "lineage": None,
                "readiness": {"table": query, "ready_partition": "", "note": ""},
                "error": f"元数据检索异常: {type(e).__name__}: {e}"}


@mcp.tool()
@_audit_tool
def ddl_generate(obj_name: str, layer: str, domain: str | None = None,
                 subject: str = None) -> dict:
    """路径 D（ETL）：从 Ontology 对象生成建表 DDL 草稿（Spark SQL，Hive 风格）。
    严格遵守数仓分层命名规范：表名 = {layer}_{domain}_{subject}_{粒度后缀}
    （后缀：明细 _di、日汇总 _1d、月汇总 _1m）；事实表统一 dt 分区。
    domain 缺省取对象自身业务域（本体治理字段）；跨域新建可显式传 domain。
    口径/类型以 Ontology 为准，生成后需人工 Review + 走审批。"""
    return generate_ddl(_onto, obj_name, layer, domain=domain, subject=subject)


@mcp.tool()
@_audit_tool
def etl_generate(obj_name: str, layer: str, domain: str | None = None,
                 subject: str = None,
                 metrics: list[str] = None,
                 dimensions: list[str] = None) -> dict:
    """路径 D（ETL）：从 Ontology 生成 Spark SQL ETL 管道草稿
    （INSERT OVERWRITE 聚合，源=DWD 明细 → 目标=汇总/应用表）。
    指标公式/required_filter/维度映射以 Ontology 为准，跨域指标自动 CTE 合并
    （与翻译引擎多指标策略一致）。domain 缺省取对象自身业务域。
    生成后需人工 Review + 走审批。"""
    return generate_etl(_onto, obj_name, layer, domain=domain, subject=subject,
                        metrics=metrics, dimensions=dimensions)


@mcp.tool()
@_audit_tool
def modeling_plan(changes: list[dict]) -> dict:
    """路径 D（方案落地）：变更清单 → 配对 DDL+ETL 列表。

    每个变更: {type: create|add_field|modify_logic|register, obj_name, layer?,
    domain?, subject?, metrics?, dimensions?}。
    工具层强制配对：create/add_field 必须 DDL 与 ETL 成对（N 表 → N 对），
    数量不一致会报错；modify_logic 仅 ETL；register 仅注册项。
    返回每项的 editable 字段供确认环节逐项编辑。"""
    return generate_modeling_plan(_onto, changes)


@mcp.tool()
@_audit_tool
def ontology_register(kind: str, entry: dict, user: str = "") -> dict:
    """路径 D（本体注册落地）：把对象/指标/关系/黑话写入本体存储。

    kind: object | function | relation | glossary | config
    entry: 与 YAML 条目结构一致的 dict（object 需 name、function 需 name+formula+owner…）
    写入目标由 DATA_AGENT_ONTOLOGY_STORE 决定：
      * yaml     —— 写 backend/ontology/*.yaml（发布基线快照，单机）
      * supabase —— 写 Supabase 表（多人编辑真源，revision 乐观锁）
      * sqlite   —— 只读产物，拒绝写入（提示写 YAML 后重新编译）

    **合并语义**（重要）：
      * 已存在条目（update）——按唯一键合并：省略字段保留原值（如 Region 省略
        object_type 仍为 dim；最小更新可只提交要改的字段）
      * 新建条目（create）——缺省字段落库默认值（status=active、object_type=fact、
        version=v1.0 等）
      * 更新 function 须携带 formula/owner（NOT NULL 列）；更新 object 无必填限制
    写入前做字段级格式校验：非法值（如 pre_aggregated 传布尔、required_filters 传
    字符串）返回 400 级错误并指明字段与期望类型，不会污染本体。

    必须在用户确认后调用（modeling-workflow 阶段 2 本体注册）；返回落库结果。"""
    kind = kind.lower()
    tables_meta: list[dict] = []
    mode = "create"
    try:
        # 判定 create/update（基于当前内存 Ontology 的唯一键存在性）
        if kind == "object":
            mode = "update" if entry.get("name") in _onto.objects else "create"
        elif kind == "function":
            mode = "update" if entry.get("name") in _onto.functions else "create"
        elif kind == "relation":
            mode = "update" if any(r.get("source") == entry.get("source") and
                                   r.get("target") == entry.get("target") and
                                   r.get("join_key") == entry.get("join_key")
                                   for r in _onto.relations) else "create"
        elif kind == "glossary":
            mode = "update" if entry.get("term", "").strip().lower() in _onto.term_index else "create"
        elif kind == "config":
            mode = "update" if entry.get("key") in ("time_dimension", "partition_column") else "create"
        # 缺陷①：写入前结构校验（字段级错误，不裸 400；update 模式只校验传入字段）
        validate_entry(kind, entry, mode=mode)
        if kind == "object":
            _writer.upsert_object(entry)
            # 自动采集关联表的元数据（路径 D：建好本体同时采好表元数据）
            tables_meta = collect_metadata_from_entry(str(DB), _onto, entry)
            if tables_meta and CONFIG.ontology_store == "supabase":
                _writer.upsert_tables(tables_meta, user=user)
        elif kind == "function":
            _writer.upsert_function(entry)
        elif kind == "relation":
            _writer.upsert_relation(entry)
        elif kind == "glossary":
            _writer.upsert_glossary(entry)
        elif kind == "config":
            _writer.upsert_config(entry["key"], entry.get("value"))
        else:
            return {"error": f"不支持的注册类型: {kind}（支持 object/function/relation/glossary/config）"}
    except EntryValidationError as e:
        return {"error": f"字段级错误: {e}",
                "hint": "已存在条目为合并更新（省略字段保留）；新建缺省落默认值；function 更新须带 formula/owner"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"本体注册失败: {e}"}
    # 注册成功后自动重载本体：当前会话后续查询立即使用最新本体。
    # reload 仅刷新内存本体，失败不应掩盖已成功的注册（网络抖动时可稍后 ontology_reload）。
    try:
        reload = _reload_ontology()
        reloaded, reload_note = True, "注册成功并已重载本体"
    except Exception as e:  # noqa: BLE001
        reload, reloaded, reload_note = {}, False, f"注册成功，但重载本体失败（{e}），可稍后调用 ontology_reload"
    return {"ok": True, "kind": kind, "mode": mode, "entry": entry,
            "store": CONFIG.ontology_store,
            "reloaded": reloaded,
            "tables_metadata": {"collected": len(tables_meta),
                                "synced": CONFIG.ontology_store == "supabase" and bool(tables_meta)},
            "note": reload_note + "；关联表元数据已自动采集"
                    + ("并入库 ontology_tables" if tables_meta and CONFIG.ontology_store == "supabase"
                       else "（yaml/sqlite 模式不写库）")}


@mcp.tool()
@_audit_tool
def ontology_reload() -> dict:
    """运行时重新加载本体（多人编辑后刷新当前会话）。

    从当前 store（yaml / supabase）重读并重建 Ontology 索引，刷新
    validator/translator/metadata。多人协作场景下，其他成员通过
    ontology_register 改了本体后，用本工具让当前会话读到最新。"""
    return _reload_ontology()


@mcp.tool()
@_audit_tool
def explore_promote(sql: str) -> dict:
    """探索固化（P2）：探索 SQL → 口径提取 → 路径 D 注册草稿。

    把探索 SQL 的 SELECT/WHERE/GROUP BY/FROM 反推为候选指标/维度/过滤/源表，
    生成 modeling_plan 的 register 变更项草稿（可编辑）：
      1. function 草稿：每个聚合表达式 → 候选指标（formula/owner/required_filters/
         supported_dimensions），status=draft（未激活）
      2. object 草稿：SQL 引用的未注册表 → 候选对象（需先建表或补映射）

    物理列 → 业务属性自动反查 Ontology field_mapping；反查不到的列
    列入 unmapped_columns（人工补，不臆造）。

    注意：生成的是**草稿**，必须经用户确认（modeling_plan 清单确认协议）后
    才可 ontology_register 固化。"""
    return build_register_draft(_onto, sql)


@mcp.tool()
@_audit_tool
def scheduler_submit(task_spec: dict) -> dict:
    """路径 D（ETL）：提交调度任务（**预留**）。当前未接入公司调度平台 MCP（mcp-scheduler），
    接入后在此补充核心逻辑：任务依赖、调度周期（cron）、告警通道、幂等键。"""
    return {"error": "调度配置未接入平台 MCP（预留）。"
                     "接入 mcp-scheduler 后：提交任务依赖/周期/告警，带幂等键。"}


@mcp.tool()
@_audit_tool
def explore_validate(sql: str) -> dict:
    """探索取数（P0/P1）：校验探索 SQL（表名白名单/只读/强制分区）。

    适用：指标未注册的探索性分析（用户明确走探索路径时）。
    校验通过返回 explore_token（绑定 SQL 指纹，120s 有效）——
    explore_execute 必须携带。探索通道与正式取数（query_token 链）物理隔离，
    但只读护栏（禁写/禁 DDL/行数上限）完全一致。

    表名白名单：SQL 引用的物理表必须已注册（来自 metadata_search /
    ontology_search / ontology_traverse 返回值），防编造表名。
    前端集成：纯 SQL 模式（Monaco + dt-sql-parser 编辑）提交后先调本工具。"""
    allowed = candidate_tables(_onto)
    result = validate_explore_sql(sql, allowed)
    if not result["ok"]:
        return {"ok": False, "errors": result["errors"],
                "tables": result["tables"],
                "hint": "仅支持已注册表；必须带 dt 分区条件；只读 SELECT/CTE"}
    result["ok"] = True
    result["explore_token"] = _explore_store.issue(sql)
    result["note"] = "探索路径生成（非注册口径），建议人工核对后使用"
    return result


@mcp.tool()
@_audit_tool
def explore_execute(sql: str, explore_token: str) -> dict:
    """探索取数（P0/P1）：执行已校验的探索 SQL（只读，行数上限 1000）。

    必须携带 explore_validate 返回的 explore_token（绑定 SQL 指纹，
    防绕过校验直接执行任意 SQL）。执行走 Executor 只读护栏
    （FORBIDDEN 正则 + SQLite mode=ro + MAX_ROWS）。

    返回结果集 + 探索标记；结果仅供观测，未固化为注册口径。"""
    ok, reason = _explore_store.verify(explore_token, sql)
    if not ok:
        return {"error": reason}
    result = _executor.execute(sql)
    if "error" in result:
        return result
    result["explore"] = True
    result["note"] = "探索路径结果（非注册口径），观测后稳定可走路径 D 固化注册"
    return result


if __name__ == "__main__":
    mcp.run(transport="stdio")
