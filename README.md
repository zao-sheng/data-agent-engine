# data-agent-engine：企业数据 Agent 引擎（DSH + Python）

基于 DeepSeek Harness 的数据查询 Agent 引擎：**意图理解 → MQL → 确定性翻译 → SQL 执行**。
零 JS 编写（引擎 100% Python），自带 order 主题数仓样例（15 张表，克隆即用）。
引擎按「理解与执行分离」设计：LLM 只负责把自然语言解析为 MQL，翻译引擎以确定性规则
生成 SQL 并执行，**不猜表名、不猜 JOIN、不猜过滤**。

## 核心概念：三个「存储」各管什么

| 存储 | 管什么 | 位置 |
|------|--------|------|
| **本体（Ontology）** | 引擎的「脑子」：有哪些表、指标怎么算、对象怎么 JOIN、黑话怎么归一 | YAML（默认）/ SQLite（编译产物）/ **Supabase（多人编辑真源）** |
| **样例库** | 引擎的「手脚」：真正的业务数据（GMV 数值等） | `backend/seed/sample.db`（SQLite） |
| **数仓（真实）** | 生产取数的数据源 | `DATA_AGENT_DSN_*`（mysql/doris/hive/sparksql） |

> **Supabase 是本体仓库，不是数据仓库**——它存「引擎认识哪些指标、口径怎么定义」，
> 业务数据本身（GMV 数值、订单明细）不经过它。换句话：Supabase 管认知，数仓管数据。

## 快速开始（3 步）

```bash
git clone <repo> && cd data-agent-engine
./install.sh --sample        # 装环境 + 生成样例库 + 自动配置 DSH + 建「数据助理」预设
# 重启 DSH → 新开会话选「数据助理」→ 提问：
#   "上个月华东区活跃用户的平均客单价，按周拆分"
```

> 首次运行需手动 `cd ~/.dsh/profiles/web && pnpm install`（安装 dsh-mcp-client 依赖）。

### 三种部署模式（选一）

| 模式 | 命令 | 本体来源 | 适用场景 |
|------|------|---------|---------|
| 样例模式 | `./install.sh --sample` | 本地 YAML | 单机演示/开发（默认） |
| 真实数仓 | `./install.sh --real` | 本地 YAML + `DATA_AGENT_DSN_*` | 接生产数仓取数 |
| Supabase 多人编辑 | `./install.sh --supabase` | 云端 Supabase（schema.sql 建表 + import 导入） | 团队共同维护本体 |

**yaml vs supabase 怎么选**：单机用 yaml（零依赖、克隆即跑）；团队多人共同编辑指标口径/
表映射时用 supabase（直写 DB + revision 乐观锁；本体维护在可视化系统 + 内置审批流，Git 中 YAML 仅作发布基线快照）。

## 能力一览

| 能力 | 说明 |
|------|------|
| 先规划后执行 | 四类意图（取数/元数据咨询/新建建模/其他操作）→ 确定性路径选择；`intent_classify` 预分类（可回归）+ LLM 兜底；取数禁止自我发挥 |
| 确定性翻译 | MQL → SQL（表选择/JOIN/权限注入/默认 t-1/多方言），零 LLM、快照测试锁定 |
| 指标族/黑话 | GMV 多口径三级识别（标注口径名）；poi→门店 等黑话归一（回答回译） |
| 多表/多指标 | 事实×维度自动 JOIN（零猜测）；同域多列 + 跨域 CTE 对齐 |
| MQL 用户确认 | confirm_token 工具层强制（防跳过确认）；query_token 防绕过翻译执行裸 SQL |
| 元数据检索 | `metadata_search`：表（15 张样例表元数据入库）/口径/血缘/就绪；Supabase 模式查 `ontology_tables` 真源，未配置回落本地 |
| 本体治理 | 对象/指标带业务域（domain：ord/usr/prd/fin…）、类型（fact/dim）、状态（active/draft/deprecated）、责任人、标签、敏感级、更新频率——跨域查询按域路由，废弃对象/表自动跳过 |
| 建模流程（路径 D） | modeling-workflow 阶段化（需求→方案→落地→测试→上线→调度→SLA/DQC）；清单确认协议（可编辑）；modeling_plan 配对 DDL+ETL（Spark SQL）；对象注册自动采集关联表元数据入 `ontology_tables` |
| 本体存储 | YAML 默认 / SQLite 产物 / Supabase 真源，换存储只动读取层（store 接口）；凭证只从 `.env` 读取，不写死在代码/脚本 |
| 安全与运维 | 双令牌 + 审计/运行日志（轮转）+ 启动自检 + health_check + 配置外置 `.env` |
| 评测门禁 | Golden Dataset 回归（29 条：翻译执行 + 意图分类）+ 134 项单测，CI 自动跑 |

## 示例提问（部署后直接问）

以下提问全部基于自带 order 样例数据（固定种子，结果可复现）。

| 提问 | 预期表现 |
|------|---------|
| 昨天的 GMV 是多少 | 标注口径：GMV（支付口径 v1.0：SUM(pay_amount)，过滤 is_valid=1，数据截至昨天） |
| 最近 30 天 GMV 和退款金额，按门店类型 | 跨域多指标：CTE 合并，两类门店都有 GMV 和退款 |
| 华东区数码类产品的 GMV，按门店城市拆分 | 自动 JOIN 产品/门店/区域三张维度表，按城市分组 |
| 华东区 poi 的 goods 销售额 | 黑话归一：poi→门店、goods→产品、销售额→GMV，回答用词跟随原话 |
| 下单 GMV 最近 30 天 | 精确命中下单口径，结果与支付口径不同 |
| GMV 有哪几种口径 | 召回支付/下单/消费三种口径，请你选择 |
| 帮我新建一个按周汇总的用户复购率指标 | 走路径 D：需求清单（可编辑）→ 方案书 → modeling_plan 配对 DDL+ETL → … → SLA/DQC |
| 查 dwd_ord_pay_di 表里 pay_amt 大于 100 的 | 物理表/字段名被校验器拒绝，不硬写物理名 |

> 每条提问都会经过：规划 → OAG 理解 → MQL 校验 → **确认环节** → 确定性翻译 → 口径标注回答。

## 架构

```
DSH 侧（配置，零代码）                Python 侧（引擎）
┌──────────────────────────┐   MCP    ┌──────────────────────────────┐
│ 数据助理 preset           │ ──────▶ │ mcp_servers/server.py        │
│  persona + 8 个 skills    │  stdio   │  ├ 意图预分类 intent_classify│
│  dsh-mcp-client           │          │  ├ 元数据检索 metadata_search│
└──────────────────────────┘          │  ├ OAG: ontology_search/traverse
                                       │  ├ MQL: mql_validate/explain │
                                       │  ├ 执行: semantic_translate  │
                                       │  ├ 执行: execute_sql（只读）  │
                                       │  ├ 建模: ddl/etl/modeling_plan
                                       │  ├ 本体: ontology_register/reload
                                       │  └ 运维: health_check / scheduler_submit(预留)
                                       └──────────────────────────────┘
本体存储：YAML（默认）→ SQLite（编译产物）→ Supabase（多人编辑真源）
数据介质：SQLite 样例库 / 远程数仓（mysql/doris/hive/sparksql）
```

```
backend/
├── seed/          样例数据生成器（schema.sql + seed.py，固定种子）
├── ontology/      Ontology 源（objects/functions/relations/glossary/config）
├── core/          确定性引擎（18 模块：translator/validator/executor/metadata/
│                  ddl_gen/etl_gen/modeling_plan/intent/ontology_store/ontology_writer/
│                  query_token/confirm_token/audit/runtime_log/startup_check/config）
├── mcp_servers/   FastMCP 入口（17 个工具）
├── supabase/      schema.sql（7 张表建表，含全部治理列）+ rls.sql（权限）
├── tests/         12 文件 138 项单测
└── eval/          Golden Dataset（29 条）+ 评测门禁
dsh-side/          setup_dsh.py + 「数据助理」预设模板 + 8 个 skills
```

## 二次开发

### 换主题域（order 只是示例）
引擎零主题耦合，换主题 = 替换 `backend/ontology/` 四份文件，代码不动：
1. `config.yaml`：改 `time_dimension` 与 `partition_column`；
2. `objects.yaml` / `functions.yaml` / `relations.yaml`：填新主题的对象/指标/关系；
3. 替换样例数据：新写 `seed/`（或用 `builder/build_ontology.py` 生成骨架）；
4. 跑评测门禁。
> 本体存储三选一：YAML（改文件）/ SQLite（`ontology_compile` 编译）/ Supabase（建表 + `ontology_import` 导入）。

### 换真实数仓
1. 复制 `backend/.env.example` 为 `backend/.env`，填 `DATA_AGENT_DSN_<方言>`；
2. 装驱动：`uv pip install --python backend/.venv/bin/python pymysql`（mysql/doris）或 `pyhive thrift sasl`（hive/sparksql）；
3. 生成/补全本体 → 填 Golden Dataset → 跑门禁。

### 接入 ETL 平台（路径 D）
`ddl_generate` / `etl_generate` / `modeling_plan` 已实现（Spark SQL）；
`scheduler_submit` 为预留占位（待接 mcp-scheduler）。

### 加指标 / 加表
编辑 `backend/ontology/functions.yaml`（formula 白名单）+ `objects.yaml`（pre_aggregated 等）+
`relations.yaml`（JOIN 键）→ 跑评测门禁。多人协作时走 `ontology_register`（Supabase 真源）。

### 行级权限
翻译引擎按 `user.region_ids` 注入 `F.region_id IN (...)`（步骤 0）。真实接入：
从权限系统查出当前用户可见区域 id，由会话上下文填入 `user` 参数即可。

## 更新与卸载

```bash
./install.sh update       # git pull → 重建样例库 → 更新 DSH 配置 → 本体编译 → 评测（单测+eval）
./install.sh uninstall    # 移除 DSH 配置/预设/样例库/venv（源码保留）
```
> 三个命令均**幂等**：重复执行安全；卸载只删除本工具创建的内容，不误删用户数据。

## 文档

- [代码解读报告（当前代码）](DataAgent代码解读报告.md)
- [数据仓库规范](dsh-side/agent-presets/data-agent/skills/warehouse-standards/SKILL.md)
- [建模流程规范](dsh-side/agent-presets/data-agent/skills/modeling-workflow/SKILL.md)
- [环境变量配置说明](backend/.env.example)
- [本体存储模式对比（yaml vs sqlite vs supabase 选型）](docs/ontology-store-modes.md)
- [指标平台 vs 本引擎架构对比报告](docs/architecture-comparison.md)


## License

MIT
