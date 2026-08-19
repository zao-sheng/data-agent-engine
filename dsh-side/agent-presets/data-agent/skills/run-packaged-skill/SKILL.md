---
name: run-packaged-skill
description: 路径 C——命中已封装 Skill 的高频模式，跳过 LLM 生成 MQL，直接结构化参数执行。
---

# 路径 C：Skill 直执行

当 plan-routing 判定「命中 Skill」时走本路径：**跳过 LLM 自由生成 MQL**，直接构造结构化参数调用。

## 已封装的 Skill 类型
| 类型 | 示例 | 内部实现 |
|------|------|---------|
| MQL 型 | query_metric（查已注册指标） | 构造 MQL → `mcp__dataagent__semantic_translate` → `execute_sql` |
| 操作型 | generate_ddl / monitor_setup | `mcp__dataagent__ddl_generate` 等平台工具 |

## 流程
1. 把用户需求映射为 Skill 的**结构化输入参数**（不是自然语言，不是自由 SQL）；
2. 按该 Skill 的 schema 校验参数；
3. 执行（MQL 型走语义层翻译；操作型走平台工具）；
4. 结果按口径标注回答（同 query-metric 第 6 步）。

## 规则
- 参数固定、输入输出明确的场景优先走本路径（0 幻觉、口径一致）；
- Skill 执行失败 → 降级到 query-metric 并标记「需人工确认」。
