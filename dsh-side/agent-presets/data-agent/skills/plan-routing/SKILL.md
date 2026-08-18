---
name: plan-routing
description: 意图识别后的计划与路径选择（Plan Agent）。任何用户请求先走本流程，再进入对应路径执行。
---

# Plan：意图分类 → 复杂度评估 → 路径选择

任何用户请求，先做规划，**确定路径后再执行**，不要直接跳到翻译/执行。

## Step 1 · 意图分类
把用户请求归入五类之一（用 `mcp__dataagent__ontology_search` 辅助判断）：
| 意图 | 判定 | 目标路径 |
|------|------|---------|
| 取数 | 查询已注册指标/对象 | A（MQL→翻译） |
| 取数（高频模式） | 命中已封装 Skill | C（Skill 直执行） |
| 建模 / 元数据咨询 | 问表结构/血缘/口径定义 | 咨询路径（用 ontology_search/traverse 回答） |
| ETL / 建表 / 新建指标 | 要新建表/管道/指标 | D（ETL + DataOps） |
| 探索性长尾 | Ontology 无对应物 | B（兜底） |

## Step 2 · 复杂度评估（L1–L4）
- **L1**：单指标、无 JOIN、命中 Skill
- **L2**：2~3 表 JOIN、标准聚合
- **L3**：跨域、需新建中间表/指标
- **L4**：新建 ETL 管道、指标体系变更（必须全程审批）

## Step 3 · 选择执行路径（决策规则）
```
有匹配 Skill？            → C（load 对应 skill 执行）
指标已注册 + 映射完整？    → A（load path-a-query skill）
需新建表/管道/指标？      → D（load path-d-etl skill）
Ontology 无对应物（探索） → B（load path-b-fallback skill）
```
- 复杂度 L1 → 执行后**结果直出**（仍标注口径）；
- L2 及以上 → MQL 生成后**必须先经用户确认**再翻译执行（见 path-a-query）；
- L3/L4 → 拆子任务、走审批（`ask_user_question`，长链路走 task-board 异步审批）。

## Step 4 · 输出执行计划
用一句话说明：意图 / 复杂度 / 路径 / 风险点 / 是否需要用户确认，然后加载对应路径的 skill。
