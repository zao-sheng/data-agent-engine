---
name: path-d-etl
description: 路径 D——新建表 / ETL 管道 / 新建指标。严格遵分数仓分层分主题与命名规范，全程审批。
---

# 路径 D：ETL 开发（建表 / 管道 / 指标）

触发：用户要新建表、新建 ETL 管道、新建/变更指标（非取数）。

## 流程（L4：全程审批；L3：关键节点审批）
1. **先查重**：`mcp__dataagent__ontology_search` 确认目标对象/指标是否已存在（防重复建设）。
2. **拆子任务**（Plan Agent 输出）：
   - subtask_1 设计表结构 → `mcp__dataagent__ddl_generate`（遵守 warehouse-standards 命名规范）
   - subtask_2 开发 ETL 代码 → 模板 + Code Review
   - subtask_3 配置调度 → `mcp__dataagent__scheduler_submit`（**预留**，接平台 mcp-scheduler）
   - subtask_4 注册 Ontology → 新增对象/指标/物理映射（`ontology/` 目录）
   - subtask_5 专家审批（`ask_user_question`；长链路走 task-board 异步审批）
3. **规范检查**：load `warehouse-standards` skill，逐项核对分层/主题/命名/字段。
4. **审批后落地**：DDL 在沙箱验证 → 发布 → 注册 Ontology。

## 当前状态（预留说明）
- `ddl_generate`：已实现（从 Ontology 对象生成 DDL 草稿，遵守命名规范）；
- `scheduler_submit`：**预留占位**——公司调度平台 MCP（mcp-scheduler）接入后补充核心逻辑
  （任务依赖 / cron / 告警通道 / 幂等键）；
- ETL 代码模板、沙箱建表执行：随平台能力接入逐步补齐。

## 硬性规则
- 命名必须遵守 `warehouse-standards`，禁止自造表名/字段名；
- 任何 DDL/调度操作前必须用户审批，禁止自动落地；
- 新建指标必须注册 Ontology 后方可被后续查询使用。
