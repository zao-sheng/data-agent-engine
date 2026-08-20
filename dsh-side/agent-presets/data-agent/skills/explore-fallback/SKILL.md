---
name: explore-fallback
description: 探索性取数（路径 E）——指标未注册时，找数据源表→写 SQL→受控执行观测，稳定后可固化注册。支持 NL / 纯 SQL / 混合三模式。
---

# 探索性取数（路径 E）

## 触发条件（满足其一即进入）
1. `intent_classify` 返回 `intent=explore`（探索信号 + 指标缺失，如"先看看支付数据长什么样"）；
2. 取数缺指标/维度 → 已询问是否新建 → 用户**拒绝新建**但仍想先看数据（明确同意走探索）。

> 指标已注册 → 走 A（query-metric）正式取数；用户要新建 → 走 D（modeling-etl）。
> 探索是"未注册口径的先看数据"，不是绕过正式链的常态路径。

## 三模式（用户可自行选择，前端 Monaco 编辑器）

### 模式 1 · 自然语言（NL）
适合：表述清晰、口径近似已有对象。
```
NL → 引擎 OAG/MQL 正式链（若可映射到近似指标）或经 explore 通道找表
```
- 先 `mcp__dataagent__intent_classify` + `metadata_search` + `ontology_search`；
- 若能映射到已注册指标 → 走 A；否则按模式 2 引导用户。

### 模式 2 · 纯 SQL（Monaco + dt-sql-parser）
适合：**多表关联 / 百行千行复杂 SQL**（自然语言难以描述）。
```
Monaco 编辑器（SQL 高亮/补全/语法校验 by dt-sql-parser）
  → 用户写/粘贴 SQL
  → mcp__dataagent__explore_validate(sql)   # 表名白名单/只读/强制 dt 分区
  → 通过 → mcp__dataagent__explore_execute(sql, explore_token)
  → 结果回编辑器，可反复修改重跑（观测迭代）
```
- 表名只能来自 `metadata_search` / `ontology_search` / `ontology_traverse` 返回值
  （工具已校验，防编造）；
- SQL 必须带 `dt` 分区条件（防全表扫描）；只读 SELECT/CTE；
- `explore_token` 绑定 SQL 指纹（120s），改 SQL 须重新 validate。

### 模式 3 · 混合（推荐给复杂探索）
适合：不知道表结构但有诉求，又不愿纯手写。
```
NL 诉求 → LLM 用工具返回的表/字段起草 SQL 骨架
  → 骨架回填 Monaco 编辑器
  → 用户人工精修（补 JOIN/过滤/聚合）
  → explore_validate → explore_execute（同模式 2）
```

## 流程（模式 2/3 通用）
1. **找表**：`metadata_search("<主题>")` + `ontology_search` + `ontology_traverse`
   拿到候选事实表 + 关联维度表 + JOIN 键（不猜表名）；
2. **写 SQL**：Monaco 编辑或 LLM 起草后人工精修；
3. **校验**：`explore_validate(sql)`——只读 + 表名白名单 + 强制 dt 分区；
   失败按 errors 修正后重试（最多 3 次）；
4. **执行观测**：`explore_execute(sql, token)` → 结果集（≤1000 行）；
   可反复修改重跑（观测迭代，看口径/数据是否符合预期）；
5. **固化反馈**：观测稳定后告知用户可走路径 D 固化——
   探索 SQL 的 SELECT/WHERE/GROUP BY 可反推候选指标公式/维度/过滤，
   供 `modeling_plan` + `ontology_register` 预填草稿。

## 禁止
- 禁止写库/DDL/任何非 SELECT 语句（explore_execute 已强制）；
- 禁止把物理表名/字段名作为「注册口径」告知用户（探索结果 = 非注册口径）；
- 禁止绕过 explore_validate 直接执行（explore_token 已强制绑定）；
- 禁止在用户未明确同意探索时自动走本路径（默认 query-metric / 结束）。

## 输出标记
所有探索结果必须声明：
「⚠️ 探索路径结果（非注册口径），仅供观测；稳定后走路径 D 固化注册。」
