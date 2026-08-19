---
name: modeling-etl
description: 路径 D——建模类需求（新建表 / ETL 管道 / 新建指标 / 改结构）。加载 modeling-workflow 规范，按「需求分析→方案→落地(配对DDL+ETL)→测试→上线→调度→SLA/DQC」阶段化执行，每阶段先出逻辑报告（可编辑）、用户确认后才真实执行。
---

# 路径 D：建模 / ETL 开发（阶段化，先报告后执行）

触发：用户提出**建模类需求**（新建表、新建/变更指标、改表结构、改 ETL 逻辑、注册本体）。

## 第一步：加载规范
```text
load modeling-workflow skill    # 阶段化流程 + 需求提取模板 + 方案书模板
load warehouse-standards skill # 分层/主题/命名规范
```

## 编排规则（必须遵守）
1. **按 modeling-workflow 的阶段 0→7 顺序执行**，不可跳步、不可倒序。
2. **每个阶段先产出逻辑报告**（模板见 modeling-workflow）→ **走「清单确认协议」**：
   先一次性输出完整清单（每项带编号）→ 再调用**一次** `ask_user_question`
   请用户整体确认或指出修改项（编号+新值）→ **确认后才调用 `mcp__dataagent__*` 真实执行**。
3. **禁止**把同一清单拆成多次单问选择题（每次只问一项、给固定选项）——那是低效且
   无法编辑的交互；用户应看到**一张可编辑的待确认清单**。
4. 信息缺失（需求识别提取表中的 ❓ 项）先与用户多轮澄清，**不得臆造口径/来源/粒度**。
5. 阶段间切换必须显式说明：「阶段 N 清单如下，请整体确认或指出修改项」。

## 阶段工具映射（确认后调用）
| 阶段 | MCP 工具 | 说明 |
|------|---------|------|
| 1 方案查重 | `mcp__dataagent__ontology_search` | 查目标对象/指标/表是否已存在 |
| 2/3 方案落地 | `mcp__dataagent__modeling_plan` | **一次生成配对 DDL+ETL 列表**（N 表=N 对，`summary.paired` 必须 True；严禁分开调 ddl/etl 造成数量不对应） |
| 2/3 本体注册 | `mcp__dataagent__ontology_register` | 对象/指标/属性/关系注册（yaml 文件 / supabase 真源双通道，确认后调用） |
| 6 调度 | `mcp__dataagent__scheduler_submit` | 预留：接入平台后真实提交 |
| 7 SLA/DQC | 治理配置登记 | 预留：接入数据质量平台后自动登记 |

## 硬性规则
- 任何 DDL/ETL/注册/调度/治理操作**必须用户确认后执行**，禁止自动落地。
- **所有确认走「清单确认协议」**：先输出完整清单（编号）→ 一次整体确认；
  禁止逐项弹选择题、禁止一次确认拆多次提问。
- **DDL 与 ETL 必须成对**：新增/加字段事实表一律用 `modeling_plan` 生成，
  工具返回 `summary.paired=False` 或 `errors` 非空时先解决，禁止直接落地不完整方案。
- 命名遵守 `warehouse-standards`；新建指标必须注册 Ontology 后方可被查询。
- 全部过程留痕（审计日志）；用户拒绝某阶段则停止并记录原因。
- 演示引擎统一 **Spark SQL**（DDL/ETL 均为 Hive 风格 Spark SQL）。
