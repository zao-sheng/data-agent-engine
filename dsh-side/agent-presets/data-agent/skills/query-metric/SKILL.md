---
name: query-metric
description: 路径 A——已注册指标的取数查询。OAG 理解 → MQL → 用户确认 → 确定性翻译 → 执行 → 口径标注回答。
---

# 路径 A：指标取数（理解与执行分离）

## 前置检查：指标/维度是否齐全（plan-routing 已判定，进入前复核）
1. 用 `mcp__dataagent__metadata_search`（或 `ontology_search`）确认用户要的
   **每个指标、每个维度**都已注册。
2. **全部齐全** → 继续下方流程。
3. **有缺失** → 明确告知用户：未找到「指标 X / 维度 Y」的注册信息 →
   `ask_user_question` 询问（**三选项**）：
   - **新建**（走 D 建模审批）→ load `modeling-etl` skill；
   - **探索看看**（走 E：直接写 SQL 统计明细，观测后稳定再固化）→
     load `explore-fallback` skill——**探索是缺指标时的默认可行路径，
     用户不想建正式指标但要看数据时首选**；
   - **都不做** → 结束，不查询不编造；
   - **严禁**用近似指标/字段替代查询（尤其跨量纲：单数 ≠ 金额），严禁自我发挥。
   - ⚠️ 确认选项里**不得出现「用近似指标代替」**——量纲不同的指标
     （如金额 vs 单数）不可替代；量纲相同但口径略异的才可作为澄清候选。

> 参考：`metric_disambiguate` 已做量纲感知（查"消费订单数"不会返回金额指标），
> 若返回 `dimension_mismatch` 提示，直接忽略该候选，不要向用户提供跨量纲选项。

## 流程（顺序执行，不可跳步）
1. **OAG 五步**：load `oag-retrieval` skill 完成实体识别 → 定位 → 关系扩展 → 组装。
2. **生成 MQL**：按 `mql-authoring` skill 生成 → `mcp__dataagent__mql_validate` 校验通过。
3. **用户确认（L2 及以上必须；L1 建议）**：
   调用 `mcp__dataagent__mql_explain` 把即将查询的**指标口径/版本/维度/过滤/时间**
   用中文向用户展示（返回 `confirm_token`），然后 `ask_user_question` 请求确认。
   - 用户确认 → 继续第 4 步（**携带 confirm_token**）；
   - 用户修改 → 据反馈改 MQL → 回到第 2 步（需重新 explain 拿新 confirm_token）；
   - 用户拒绝 → 结束，不执行。
4. **确定性翻译**：`mcp__dataagent__semantic_translate(mql, confirm_token=…)` ——
   confirm_token 必填，来自第 3 步 explain；MQL 与确认时不一致会被拒绝。
5. **执行**：`mcp__dataagent__execute_sql(sql, query_token=…)` ——query_token 来自
   第 4 步翻译返回，禁止绕过翻译引擎传裸 SQL。
6. **口径标注回答**（强制格式）：
   ```
   指标 X（口径 vX.X：公式；必要过滤；数据区间；数据截至）结果：...
   ```
7. **结果展示（数据必须用表格）**：`execute_sql` 返回的 `rows` 用 Markdown 表格
   详细展示，禁止文本罗列：
   - `columns` 作表头，`rows` 逐行渲染（≤1000 行，有多少行展示多少行）；
   - 表头与数据之间必须有分隔行 `|---|---|`；
   - 表格上方注明「共 N 行」（引用返回的 `row_count`）；`truncated=true` 时
     标注「已截断（仅前 1000 行）」；
   - **禁止自我统计**：行数引用返回的 `row_count`，不自己数；确需观察分布时
     基于表格逐条核对后再给结论，数不清就只展示表格；
   - 数值保留返回原样（小数位不变），不加千分位改写。

## 硬性规则
- 第 3 步的「用户确认」是**安全与口径对齐的关键闸门**，禁止跳过（除非用户明确说"直接执行"）。
- `semantic_translate` 缺 confirm_token 或 MQL 变更后会失败——必须重新 `mql_explain`。
- 翻译引擎输出的 SQL 直接执行，不要改写、不要拼接。
- 回答中标注 `metric_version`、数据水印（时间范围 + 是否默认 t-1）。
- 缺指标/维度只允许「告知缺失 + 询问是否新建」，禁止近似替代。
