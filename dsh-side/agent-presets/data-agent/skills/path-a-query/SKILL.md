---
name: path-a-query
description: 路径 A——已注册指标的取数查询。OAG 理解 → MQL → 用户确认 → 确定性翻译 → 执行 → 口径标注回答。
---

# 路径 A：指标取数（理解与执行分离）

## 流程（顺序执行，不可跳步）
1. **OAG 五步**：load `oag-retrieval` skill 完成实体识别 → 定位 → 关系扩展 → 组装。
2. **生成 MQL**：按 `mql-authoring` skill 生成 → `mcp__dataagent__mql_validate` 校验通过。
3. **用户确认（L2 及以上必须；L1 建议）**：
   调用 `mcp__dataagent__mql_explain` 把即将查询的**指标口径/版本/维度/过滤/时间**
   用中文向用户展示，然后 `ask_user_question` 请求确认。
   - 用户确认 → 继续第 4 步；
   - 用户修改 → 据反馈改 MQL → 回到第 2 步；
   - 用户拒绝 → 结束，不执行。
4. **确定性翻译**：`mcp__dataagent__semantic_translate`（自动带当前用户行级权限）。
5. **执行**：`mcp__dataagent__execute_sql`。
6. **口径标注回答**（强制格式）：
   ```
   指标 X（口径 vX.X：公式；必要过滤；数据区间；数据截至）结果：...
   ```

## 硬性规则
- 第 3 步的「用户确认」是**安全与口径对齐的关键闸门**，禁止跳过（除非用户明确说"直接执行"）。
- 翻译引擎输出的 SQL 直接执行，不要改写、不要拼接。
- 回答中标注 `metric_version`、数据水印（时间范围 + 是否默认 t-1）。
