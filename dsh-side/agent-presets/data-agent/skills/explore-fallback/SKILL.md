---
name: explore-fallback
description: 探索性取数（路径 E）——指标未注册时，找数据源表→写 SQL→受控执行观测，稳定后可固化注册。支持 NL / 纯 SQL / 混合三模式。
---

# 探索性取数（路径 E）

## 零前端 · 会话内交互（推荐，无需 Monaco 集成）

不需要独立前端——**直接在会话里完成探索**，三种方式任选：

### 方式 A · 直接贴 SQL（最简单）
用户在会话输入框直接粘贴/编写 SQL（支持裸 SQL 或 ```` ```sql ```` 代码块）：
```
用户: SELECT F.region_id, SUM(F.pay_amt) AS amt
      FROM dwd_ord_pay_di F
      JOIN dim_region R ON R.region_id = F.region_id
      WHERE F.dt BETWEEN '20260801' AND '20260817'
      GROUP BY F.region_id

Agent: intent_classify 识别为 explore（path=E，SQL 输入信号）
  → explore_validate(sql) 校验（表名白名单/只读/强制分区）
  → explore_execute(sql, token) 执行 → 返回结果表
```
- 引擎自动识别 SQL 输入（以 SELECT/WITH 开头 + FROM），不走 NL 解析；
- 校验失败会给出具体 errors（未注册表/缺分区/写操作），用户改 SQL 重贴即可；
- 结果标注「⚠️ 探索路径结果（非注册口径）」。

### 方式 B · 自然语言描述（NL）
用户用 NL 说诉求（"先看看华东区支付金额分布"）：
- 引擎识别 explore 意图 → 找表（metadata_search/traverse）→ **LLM 起草 SQL**
- 把 SQL 以代码块展示给用户 → 询问"确认执行 / 修改后执行"（ask_user_question）
- 用户确认 → explore_validate → explore_execute

### 方式 C · 混合（推荐复杂分析）
```
NL 诉求 → LLM 起草 SQL 骨架（基于工具返回的表/字段）
  → 代码块展示给用户 → 用户可要求修改（"再加门店维度"）
  → 确认后 explore_validate → explore_execute
```
用户不需要懂完整 SQL 结构，通过对话增量修改（"加个城市维度/按周聚合"）。

## 触发条件（满足其一即进入）
1. `intent_classify` 返回 `intent=explore`（探索信号 + 指标缺失，或 **会话内 SQL 输入**）；
2. 取数缺指标/维度 → 已询问是否新建 → 用户**拒绝新建**但仍想先看数据（明确同意走探索）。

> 指标已注册 → 走 A（query-metric）正式取数；用户要新建 → 走 D（modeling-etl）。
> 探索是"未注册口径的先看数据"，不是绕过正式链的常态路径。

## 通用流程（方式 A/B/C 共用）
1. **找表**：`metadata_search("<主题>")` + `ontology_search` + `ontology_traverse`
   拿到候选事实表 + 关联维度表 + JOIN 键（不猜表名；贴 SQL 时此步跳过）；
2. **写/取 SQL**：
   - 方式 A：直接用用户贴的 SQL；
   - 方式 B/C：LLM 起草后以代码块展示，用户确认或要求修改；
3. **校验**：`explore_validate(sql)`——只读 + 表名白名单 + 强制 dt 分区；
   失败按 errors 修正后重试（最多 3 次）；
4. **执行观测**：`explore_execute(sql, token)` → 结果集（≤1000 行）；
   可反复修改重跑（观测迭代，看口径/数据是否符合预期）；
5. **固化（P2 桥接）**：观测稳定后 → `explore_promote(sql)` 自动提取口径草稿
   （聚合表达式→候选指标公式、GROUP BY→维度、WHERE→required_filters、
   源表→source_tables；物理列自动反查业务属性）：
   - 草稿为 `status=draft`（未激活），**必须经用户确认**（modeling_plan 清单确认）；
   - 确认后 `ontology_register` 固化（公式用业务属性名，翻译引擎自动映射回物理列）；
   - 未映射的物理列会列入 unmapped_columns（人工补 field_mapping，不臆造）。

## 禁止
- 禁止写库/DDL/任何非 SELECT 语句（explore_execute 已强制）；
- 禁止把物理表名/字段名作为「注册口径」告知用户（探索结果 = 非注册口径）；
- 禁止绕过 explore_validate 直接执行（explore_token 已强制绑定）；
- 禁止在用户未明确同意探索时自动走本路径（默认 query-metric / 结束）。

## 输出标记
所有探索结果必须声明：
「⚠️ 探索路径结果（非注册口径），仅供观测；稳定后走路径 D 固化注册。」
