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
- `intent=query` 且 `metrics_missing=true` → 进入 Step 2 的「缺指标分支」。

**unclear / low 置信 → LLM 兜底裁决（不要直接问用户）**：
- intent_classify 只做**确定性预分类**，词表覆盖不到的表述（新说法、省略句、
  跨域组合、口语化）会落入 unclear/low——这是预期行为，不代表无法处理；
- 收到 unclear/low 时，先结合以下信息**自己判断**，能确定就直接走对应路径：
  1. `normalized_text`（黑话归一后文本）与 `evidence`（部分信号命中）；
  2. **会话上下文**：本会话已确认的指标/维度/口径、用户上一条请求的意图；
  3. 通用语义推断：数值/时间词倾向取数、口径/来源词倾向元数据、
     新建/开发动词倾向建模、改/调倾向存量操作。
- 只有**确实无法确定**（无任何信号、无上下文、语义明显歧义）时，才向用户澄清
  （取数/元数据/建模/其他），并给出你倾向的判断供用户确认，而非空泛地让用户重述。
- LLM 兜底裁决后，仍需遵守对应路径的安全护栏（取数走 A、缺指标问是否新建、
  建模走 D 阶段化、元数据不产数值）。

> 证据是意图分类的依据：`evidence` 里列了命中指标/对象/词表信号，回答时可按需引用；
> 若你的判断与 intent_classify 冲突，**先结合上下文复核**而不是直接推翻；
> 复核后仍认为 intent_classify 有误 → 以你的判断为准但须在计划里注明理由。

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
取数意图（intent=query 或 explore）
  ├─ metrics_missing=false（指标已注册）？
  │    是 → A（load query-metric skill，正常取数）
  │    否 → 明确告知用户：未找到「指标 X / 维度 Y」的注册信息
  │          ⚠️ 必须先 ask_user_question「是否新建」，禁止跳过该询问直接探索——
  │          用户可能更想注册为正式指标（走 D），而非临时探索
  │           ├─ 是 → D（load modeling-etl skill，走建模全流程）
  │           └─ 否 → 询问是否先探索性看看数据（ask_user_question）
  │           │         ├─ 是 → E（load explore-fallback skill：
  │           │         │      找表 → 起草 SQL → 用户确认/编辑 → explore_validate
  │           │         │      → explore_execute → 观测；稳定后回 D 固化）
  │           │         └─ 否 → 结束，不查询不编造
  │           └─（不得自行降级/拼接/臆造未注册的指标或维度）
  └─ intent=explore（探索信号 或 **会话内 SQL 输入**）→ 直接走 E
     （explore-fallback）：零前端会话内交互——贴 SQL / NL 起草 / 混合
```

> 探索意图信号：`intent_classify` 返回 `intent=explore`——
> ① "先看看/大概/探索/摸底/试试看"等探索词 + 指标未命中（`explore_mode=nl`）；
> ② **用户直接粘贴 SQL**（SELECT/WITH 开头 + FROM，含 ```sql 代码块，
>    `explore_mode=direct`）；
> ③ **起草 SQL/骨架**类词（起草/骨架/生成 SQL/写个 SQL…，`explore_mode=mixed`）。
> 三者都走路径 E（explore-fallback），会话内完成，无需前端。
> **mode 决定交互方式**：direct=贴 SQL 直接执行；nl=NL 起草后确认；
> mixed=先展示骨架等用户修改，不得直接执行。
> 这是「未注册口径的先看数据」，不是常态取数。

## Step 3 · 选择执行路径（确定性规则，按序判定）

```
① intent=query？
     → 指标+维度齐全 → A（query-metric）
     → 缺失 → 询问是否新建 → 是走 D / 否→询问是否探索 → 是走 E / 否结束
② intent=explore？（探索信号命中，如"先看看XX数据"）
     → E（explore-fallback）：找表 → 写 SQL(Monaco) → explore_validate
       → explore_execute → 观测；稳定后告知可走 D 固化
③ intent=metadata？
     → metadata_search（+ ontology_search/traverse 补充解读）
     → mixed 含 query → 先答口径，再询问是否取数
④ intent=modeling？
     → D（modeling-etl → modeling-workflow）
     → ⚠️ 进入 D 后【第一步】必须先输出完整的需求识别清单（编号表格），
       再一次性 ask_user_question 整体确认——不得先逐项追问
⑤ intent=operation（改 ETL/调度等）？
     → 直接调用对应 MCP，遵守工具约束与审批；涉及口径变更仍需用户确认
⑥ intent=unclear 或证据冲突？
     → 向用户澄清意图，不臆断；不得猜测路径继续走
```

## 硬性规则
- **先 intent_classify 拿证据，再裁决**；unclear/low 是**预分类未覆盖**（非错误），
  须由你结合上下文**兜底裁决**（见 Step 0），只有确实无法确定才向用户澄清。
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
