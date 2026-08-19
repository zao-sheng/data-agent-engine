---
name: plan-routing
description: 意图识别后的计划与路径选择（Plan Agent）。任何用户请求先走本流程，再进入对应路径执行。取数必须走规范路径，不允许自我发挥。
---

# Plan：意图分类 → 路径选择

任何用户请求，先做规划，**确定路径后再执行**，不要直接跳到翻译/执行。

## Step 0 · 确定性预分类（intent_classify，先于一切）

调用 `mcp__dataagent__intent_classify("<用户原句>")`，拿到结构化证据：

```json
{
  "intent": "query|metadata|modeling|operation|unclear",
  "confidence": "high|medium|low",
  "path": "A|metadata|D|direct|clarify",
  "normalized_text": "...", "metric_hits": [...], "object_hits": [...],
  "evidence": [{"type": "...", "value": "...", "signal": "..."}],
  "mixed": [{"intent": "...", "reason": "..."}],
  "metrics_missing": true|false,
  "note": "处置提示"
}
```

**基于证据裁决，不凭感觉分类**：
- `confidence` 为 high/medium → **采信** intent 与 path，进入 Step 2；
- `mixed` 非空 → 先执行**主 intent**，回答末尾询问另一诉求是否一并处理；
- `intent=unclear`（含 `path=clarify`）→ **向用户澄清意图**（取数/元数据/建模/其他），
  禁止猜一个路径继续走；
- `intent=query` 且 `metrics_missing=true` → 进入 Step 2 的「缺指标分支」。

> 证据是意图分类的依据：`evidence` 里列了命中指标/对象/词表信号，回答时可按需引用；
> 若你的判断与 intent_classify 冲突，**先澄清**而不是直接推翻。

## Step 1 · 意图分类对照表（四类，结合证据复核）

| 意图 | 证据特征（intent_classify 信号） | 路径 |
|------|---------|------|
| **取数** | 命中注册指标 + 数值/时间词（多少/最近/昨天…） | A → `query-metric`（缺指标维度则转 D） |
| **元数据咨询** | 命中口径/公式/哪张表/血缘/加工/就绪/字段等结构词 | 元数据检索（`metadata_search`，必要时 `ontology_search/traverse` 补充） |
| **新建/建模** | 命中新建/创建/建表/加字段等建模动词 | D → `modeling-etl` → `modeling-workflow` |
| **其他操作** | 命中修改类动词（改/调整/暂停/重跑）+ 存量对象提及 | 直接调用对应 MCP（`etl_generate`/`scheduler_submit` 等），按工具约束执行 |

> 判定要点：**取数 vs 元数据咨询**的区别是「要数值结果」还是「要元信息」。
> 用户说"查 GMV" = 取数；用户说"GMV 怎么算/哪张表有 GMV" = 元数据咨询。

## Step 2 · 取数子流程（关键分支：缺指标维度 → 询问是否新建）

```
取数意图（intent=query）
  ├─ metrics_missing=false（指标已注册）？
  │    是 → A（load query-metric skill，正常取数）
  │    否 → 明确告知用户：未找到「指标 X / 维度 Y」的注册信息
  │           ├─ 询问是否新建（ask_user_question）
  │           │    ├─ 是 → D（load modeling-etl skill，走建模全流程）
  │           │    └─ 否 → 结束，不查询不编造
  │           └─（不得自行降级/拼接/臆造未注册的指标或维度）
```

## Step 3 · 选择执行路径（确定性规则，按序判定）

```
① intent=query？
     → 指标+维度齐全 → A（query-metric）
     → 缺失 → 询问是否新建 → 是走 D / 否结束
② intent=metadata？
     → metadata_search（+ ontology_search/traverse 补充解读）
     → mixed 含 query → 先答口径，再询问是否取数
③ intent=modeling？
     → D（modeling-etl → modeling-workflow）
     → ⚠️ 进入 D 后【第一步】必须先输出完整的需求识别清单（编号表格），
       再一次性 ask_user_question 整体确认——不得先逐项追问
④ intent=operation（改 ETL/调度等）？
     → 直接调用对应 MCP，遵守工具约束与审批；涉及口径变更仍需用户确认
⑤ intent=unclear 或证据冲突？
     → 向用户澄清意图，不臆断；不得猜测路径继续走
```

## 硬性规则
- **先 intent_classify 拿证据，再裁决**；低置信（unclear）必须澄清，禁止猜路径。
- **取数必须走 A 流程**（OAG → MQL → 确认 → 翻译 → 执行），不允许自我发挥、
  不允许绕过工具直接编造结果。
- **缺指标/维度**：只告知缺失 + 询问是否新建，**不得**用近似指标/字段替代查询。
- **混合意图**：先执行主意图，末尾提示另一诉求并询问是否一并处理，不硬塞进一类。
- 元数据咨询不产生数值结果，只解读元信息（口径/血缘/就绪时间）。
- 新建类需求必须走 D 的阶段化流程（先报告 → 确认 → 执行）。
- 存量修改（改 ETL/调度）直接调工具，但涉及口径变更仍需用户确认。

## Step 4 · 输出执行计划（结构化，供下游交叉验证）

输出一句话执行计划，格式固定：

```
意图={query|metadata|modeling|operation} 路径={A|metadata|D|direct}
证据={metric_hits / 命中词表信号} 缺指标={是|否} 需确认={是|否} 混合={无|...}
```

然后加载对应路径的 skill 或调用对应工具。query-metric 的前置查重结果若与
计划中的证据冲突（如计划说命中 gmv 但查不到）→ 停下并显式报错，不静默重走。
