"""意图识别（确定性预分类）：plan-routing Step1 的结构化信号。

背景：意图分类（取数/元数据咨询/新建建模/其他操作）原为纯 prompt 判定，
不可单测、无证据。本模块把分类做成**确定性规则 + 信号打分**：
  * 实体识别：复用 Ontology（functions/objects/display_name/aliases）与
    normalize_terms 归一结果，检出问题中命中的指标/对象
  * 词表信号：取数词 / 元数据结构词 / 建模动词 / 存量修改动词
  * 判定优先级（确定性，按序）：建模 → 存量操作 → 元数据 → 取数 → 不明确
  * 输出证据链（evidence）+ 置信度 + 混合意图（mixed）+ 路径（path）

设计约束：
  * 零 LLM：本模块可单测、可入 eval 门禁（P0）
  * LLM 消费：DSH 侧基于返回的 intent/evidence/confidence 做最终裁决；
    信号冲突或低置信（unclear）时必须澄清，不得猜
  * 与下游联动：query 且指标未命中 → missing_metrics，plan-routing 据此
    走「询问是否新建」；建模 → 路径 D 阶段化；存量修改 → 直接调工具
"""
from __future__ import annotations

from .ontology_loader import Ontology

# ── 取数倾向信号（qv）：数值/时间/查询动词 ──────────────────────────
QUERY_WORDS = [
    "多少", "多少钱", "多少元", "金额", "数值", "排名", "排行", "top",
    "对比", "同比", "环比", "销量", "总额", "统计一下", "拉一下", "查一下",
    "查查", "看看", "给我", "最近", "近30天", "近7天", "近90天", "昨天",
    "今天", "上月", "上个月", "本月", "这个月", "上周", "本周", "今年", "去年",
]

# ── 强取数信号（混合意图检测用）：明确要数值结果的动词/数值词 ─────────
# 与 QUERY_WORDS 的区别：不含纯时间词——"昨天的 GMV 怎么算" 仍是口径咨询，
# 不应被标记为混合意图；只有"拉一下/多少/查一下"这类明确取数动词才算。
QUERY_STRONG = [
    "拉一下", "查一下", "查查", "看看", "统计一下", "给我", "多少",
    "多少钱", "多少元", "排名", "排行", "top", "对比", "同比", "环比",
]

# ── 元数据结构词（mv）：口径/血缘/表/就绪/加工 咨询 ──────────────────
METADATA_WORDS = [
    "怎么算", "如何算", "怎么计算", "如何计算", "计算逻辑", "口径", "公式",
    "定义", "哪张表", "哪些表", "表结构", "字段", "血缘", "依赖", "就绪",
    "更新时间", "更新频率", "加工", "怎么来的", "如何生成", "出自", "来源",
    "什么含义", "含义", "解释一下", "有哪些", "这张表", "那个表", "是什么",
]

# ── 建模动词（modv）：新建/加字段/开发 类（避免裸「建/新增」误伤）──────
MODELING_WORDS = [
    "新建", "创建", "建一个", "建一张", "建个", "帮我建", "建表", "加字段",
    "加个字段", "新增字段", "新增一个", "开发", "建模", "设计一个", "注册一个",
    "写一个", "写个",
]

# ── 存量修改动词（opv）：改/调整/暂停/重跑 类（须有存量提及才判 operation）─
OPERATION_WORDS = [
    "修改", "改一下", "改到", "改成", "调整", "暂停", "重跑", "更新任务",
    "变更", "改配置", "改口径", "改逻辑", "改调度", "修改调度",
]

# ── 存量提及信号（obj_sig）：ETL/调度/任务/配置 等运维对象 ────────────
OPERATION_OBJECTS = ["etl", "调度", "任务", "配置"]

# ── 探索信号（explore）：指标未注册时的探索性取数诉求 ────────────────
# 与 QUERY_WORDS 的区别：不含明确数值词（"多少/排名"），而是"先看看数据/
# 大概了解/试试"这类探索性表述。命中后（且指标缺失）→ 路径 E（explore）。
EXPLORE_WORDS = [
    "先看看", "看看数据", "大概看看", "探索", "探索一下", "试试看", "试一下",
    "随便看看", "有哪些数据", "数据长什么样", "先了解", "摸底", "探一下",
    "粗略", "大概", "初步看", "看下趋势", "看看趋势",
]

# ── 混合模式信号（explore 的 mode=mixed）：用户想要「NL 起草 SQL 骨架 + 人工精修」──
# 命中这些词 → 探索应走方式 C（LLM 起草骨架 → 代码块展示 → 用户对话增量修改），
# 而非直接执行（方式 B）或贴 SQL（方式 A）。
# 注意：避免与 OPERATION_WORDS（改配置/改口径…）冲突——"我改一下"太宽泛，
# 必须带 sql/骨架 等上下文才判 mixed。
MIXED_WORDS = [
    "起草", "起草个sql", "起草个 SQL", "生成sql", "生成 SQL", "写个sql", "写个 SQL",
    "sql骨架", "SQL骨架", "骨架", "给我sql", "给我 SQL", "先给我个sql",
    "sql我改", "SQL我改", "sql我改改", "SQL我改改", "帮我写sql", "帮我写 SQL",
    "生成一段sql", "生成一段 SQL", "试试写", "试着写",
]

# 意图 → 路径（与 plan-routing 的路径命名一致）
PATH_BY_INTENT = {
    "query": "A",          # 取数 → query-metric
    "metadata": "metadata",  # 元数据咨询 → metadata_search
    "modeling": "D",       # 新建/建模 → modeling-etl → modeling-workflow
    "operation": "direct", # 其他操作 → 直接调用对应 MCP
    "explore": "E",        # 探索性取数 → explore-fallback（找表→SQL→观测）
    "unclear": "clarify",  # 低置信 → 澄清，不猜
}


def _is_sql_input(text: str) -> bool:
    """检测用户输入是否为直接粘贴的 SQL（探索通道：会话内贴 SQL）。

    特征：以 SELECT/WITH 开头（可含前导空格/换行/markdown 代码块围栏
    ```sql ... ```），且含 FROM 子句（行首/空格前/换行前均可）。
    这是「用户在会话中直接写/粘贴探索 SQL」的强信号——走探索执行通道
    （explore_validate → explore_execute），而非 NL 语义解析。
    """
    head = text.strip()
    # 剥离 markdown 代码块围栏：```sql\n...\n```
    if head.startswith("```"):
        lines = head.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            head = "\n".join(lines[1:-1])
        else:
            head = "\n".join(lines[1:])
        head = head.strip()
    head = head.lstrip("`").strip().lower()
    if not head.startswith(("select ", "select\n", "with ", "with\n")):
        return False
    # FROM 子句：行首（\nfrom）或空格前（ from）均算
    return " from " in head or "\nfrom" in head or head.endswith(" from")


def _substring_hits(words: list[str], low: str) -> list[str]:
    """返回 low 中命中的词（去重保序）。"""
    seen, out = set(), []
    for w in words:
        if w.lower() in low and w not in seen:
            seen.add(w)
            out.append(w)
    return out


def _metric_hits(onto: Ontology, low: str) -> list[str]:
    """指标命中：functions 名 + display_name（去括号部分，如「GMV（支付口径）」→GMV）。
    normalize_terms 已把黑话（成交额→gmv）归一为标准名，直接对归一后文本匹配即可。"""
    hits = []
    for name, fn in onto.functions.items():
        if name.lower() in low:
            hits.append(name)
        disp = (fn.get("display_name") or "").split("（")[0].strip()
        if disp and disp.lower() in low and name not in hits:
            hits.append(name)
    return sorted(hits)


def _object_hits(onto: Ontology, low: str) -> list[str]:
    """对象命中：对象名 + 中文 display_name + aliases（英文名对归一文本匹配）。"""
    hits = []
    for name, o in onto.objects.items():
        keys = {name, o.get("display_name", "")}
        keys.update(o.get("aliases", []))
        if any(k and k.lower() in low for k in keys):
            hits.append(name)
    return sorted(hits)


def classify_intent(onto: Ontology, text: str) -> dict:
    """确定性意图分类。

    返回：
      intent         — query / metadata / modeling / operation / explore / unclear
                       （explore 含两种来源：探索信号命中 或 会话内直接粘贴 SQL）
      confidence     — high / medium / low
      path           — A / metadata / D / direct / E / clarify
      normalized_text— 术语归一后的文本
      metric_hits / object_hits — 命中实体（证据）
      evidence       — [{type, value, signal}] 证据链（供 LLM 引用/下游交叉验证）
      mixed          — [{intent, reason}] 混合意图（主 intent 之外的诉求）
      missing_metrics— query 且指标未命中时列出（plan-routing 据此询问是否新建）
      note           — 给 LLM 的处置提示
    """
    norm, _mappings = onto.normalize_terms(text)
    low = norm.lower()

    metric_hits = _metric_hits(onto, low)
    object_hits = _object_hits(onto, low)
    qv = _substring_hits(QUERY_WORDS, low)
    mv = _substring_hits(METADATA_WORDS, low)
    modv = _substring_hits(MODELING_WORDS, low)
    opv = _substring_hits(OPERATION_WORDS, low)
    ev = _substring_hits(EXPLORE_WORDS, low)
    mxv = _substring_hits(MIXED_WORDS, low)
    obj_sig = bool(metric_hits or object_hits or _substring_hits(OPERATION_OBJECTS, low))

    evidence: list[dict] = []
    for m in metric_hits:
        evidence.append({"type": "metric_hit", "value": m, "signal": "metric"})
    for o in object_hits:
        evidence.append({"type": "object_hit", "value": o, "signal": "object"})
    for w in qv:
        evidence.append({"type": "query_word", "value": w, "signal": "query"})
    for w in mv:
        evidence.append({"type": "metadata_word", "value": w, "signal": "metadata"})
    for w in modv:
        evidence.append({"type": "modeling_word", "value": w, "signal": "modeling"})
    for w in opv:
        evidence.append({"type": "operation_word", "value": w, "signal": "operation"})
    for w in ev:
        evidence.append({"type": "explore_word", "value": w, "signal": "explore"})
    for w in mxv:
        evidence.append({"type": "mixed_word", "value": w, "signal": "mixed"})

    mixed: list[dict] = []
    intent: str
    note: str
    explore_mode: str = "nl"  # 探索交互模式：nl（NL 起草）| direct（贴 SQL）| mixed（NL 起草+人工精修）

    # ① 会话内直接粘贴 SQL（探索执行通道的最高优先信号）
    if _is_sql_input(text):
        intent = "explore"
        explore_mode = "direct"
        note = ("检测到会话内 SQL 输入：走探索执行通道 E（explore_validate 校验 → "
                "explore_execute 执行）——不经过 NL 语义解析；若含未注册表名会拒绝")
    # ② 起草 SQL/骨架信号（方式 C 混合：NL 起草 + 人工精修）
    elif mxv and not metric_hits:
        intent = "explore"
        explore_mode = "mixed"
        note = ("探索意图（混合模式）：命中「起草 SQL/骨架」信号——先找表，LLM 起草 SQL "
                "骨架以代码块展示，用户对话增量修改（加维度/过滤/聚合）后再执行；"
                "不得直接执行未确认的 SQL")
    # ③ 建模（新建类动词，优先级最高）
    elif modv:
        intent = "modeling"
        note = "新建/建模意图：走路径 D（modeling-etl → modeling-workflow），先输出需求清单再整体确认"
    # ④ 探索（探索信号 + 指标缺失：未注册指标的探索性取数）
    elif ev and not metric_hits:
        intent = "explore"
        note = ("探索意图：指标未注册，走探索路径 E（explore-fallback）——找表 → 写 SQL → "
                "受控执行观测；探索结果稳定后可走路径 D 固化注册")
    # ⑤ 存量操作（修改类动词 + 存量提及，防止裸"改"误判）
    elif opv and obj_sig:
        intent = "operation"
        note = "存量操作：直接调用对应 MCP 工具（etl_generate / scheduler_submit 等），涉及口径变更仍需用户确认"
    elif opv and not obj_sig:
        intent = "unclear"
        note = "检测到修改类动词但无存量对象提及：结合上下文判断改什么，无法确定再澄清"
    # ⑥ 元数据咨询（结构词命中；若同时有强取数动词 → 混合，主意图仍为 metadata）
    elif mv:
        intent = "metadata"
        qs = _substring_hits(QUERY_STRONG, low)
        if qs:
            mixed.append({"intent": "query",
                          "reason": f"同时命中强取数信号: {qs[:3]}"})
        note = "元数据咨询：走 metadata_search（+ ontology_search/traverse 补充），不产生数值结果；" \
               + ("检测到混合取数诉求，先答口径再询问是否取数" if mixed else "")
    # ⑦ 取数（指标命中；或取数词+时间/数值信号，指标缺失时标注 missing）
    elif metric_hits:
        intent = "query"
        note = "取数意图：路径 A（OAG → MQL → 确认 → 翻译 → 执行）"
    elif qv:
        intent = "query"
        note = "取数意图但未命中注册指标：先告知缺失并询问是否新建，禁止近似替代"
    # ⑧ 低置信
    else:
        intent = "unclear"
        note = "无足够信号：结合归一化文本与会话上下文由 LLM 兜底裁决意图，确实无法确定再向用户澄清（给出倾向判断）"

    # 置信度：证据条数 ≥2 且存在主意图专属信号 → high；1 类证据 → medium；否则 low
    if intent == "unclear":
        confidence = "low"
    elif len(evidence) >= 2:
        confidence = "high"
    elif len(evidence) == 1:
        confidence = "medium"
    else:
        confidence = "low"

    missing_metrics: list[str] = []
    metrics_missing = intent in ("query", "explore") and not metric_hits
    if metrics_missing:
        missing_metrics = sorted(onto.functions)

    return {
        "intent": intent,
        "confidence": confidence,
        "path": PATH_BY_INTENT[intent],
        "normalized_text": norm,
        "metric_hits": metric_hits,
        "object_hits": object_hits,
        "evidence": evidence,
        "mixed": mixed,
        "explore_mode": explore_mode,
        "metrics_missing": metrics_missing,
        "missing_metrics": missing_metrics,
        "note": note,
    }
