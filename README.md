# data-agent-engine：order 主题 Data Agent（DSH + Python）

基于 DeepSeek Harness 的数据查询 Agent 引擎：**OAG 理解 → MQL → 确定性翻译 → SQL 执行**。
零 JS 编写（引擎 100% Python），自带 order 主题数仓样例（15 张表，DWD/DWS/ADS/DIM 分层），
克隆即用。引擎按「理解与执行分离」设计：LLM 只负责把自然语言解析为 MQL，翻译引擎以
确定性规则生成 SQL 并执行，**不猜表名、不猜 JOIN、不猜过滤**。

## 快速开始（3 步）

```bash
git clone <repo> && cd data-agent-engine
./install.sh --sample        # 装环境 + 生成样例库 + 自动配置 DSH + 建「数据助理」预设
# 重启 DSH → 新开会话选「数据助理」→ 提问：
#   "上个月华东区活跃用户的平均客单价，按周拆分"
```

> 首次运行需手动 `cd ~/.dsh/profiles/web && pnpm install`（安装 dsh-mcp-client 依赖）。

## 示例提问（部署后直接问）

以下提问全部基于自带 order 样例数据（固定种子，结果可复现），部署完成即可逐条测试，
按「预期表现」核验系统行为。

### 基础取数
| 提问 | 预期表现 |
|------|---------|
| 昨天的 GMV 是多少 | 回答标注口径：GMV（支付口径 v1.0：SUM(pay_amount)，过滤 is_valid=1，数据截至昨天） |
| 最近 7 天 GMV 是多少 | 返回**单行总和**（跨分区聚合），不是 7 行 |
| 最近 30 天退款金额是多少 | 走退款域，标注「退款不冲减 GMV」 |
| 上个月 GMV 是多少 | 自动解析 last_month 区间 |

### 多指标
| 提问 | 预期表现 |
|------|---------|
| 最近 30 天 GMV 和客单价，按门店类型 | 同域多指标：结果含 gmv / avg_order_amount / store_type 三列，直营/加盟各一行 |
| 最近 30 天 GMV 和退款金额，按门店类型 | 跨域多指标：CTE 合并，两类门店都有 GMV 和退款 |
| 最近 7 天 GMV、订单量、退款金额 | 跨域无维度：单行三列标量 |

### 多表关联
| 提问 | 预期表现 |
|------|---------|
| 华东区数码类产品的 GMV，按门店城市拆分，最近 30 天 | 自动 JOIN 产品/门店/区域三张维度表，按城市分组 |
| 各产品类型的订单量 Top5，最近 90 天 | 排序 + limit 5 |
| 最近 7 天各支付渠道的 GMV | 按 wechat/alipay/card 分组 |

### 时间与黑话
| 提问 | 预期表现 |
|------|---------|
| 客单价最近 30 天按周拆分 | 按 `YYYY-Wxx` 周分组 |
| 华东区 poi 的 goods 销售额，按城市，最近 30 天 | 黑话归一：poi→门店、goods→产品、销售额→GMV，回答用词跟随你的原话 |
| GMV 是多少（不给时间） | 回答标注「默认 t-1（昨天）」 |

### 口径与澄清
| 提问 | 预期表现 |
|------|---------|
| 下单 GMV 最近 30 天 | 精确命中下单口径（order_gmv），回答标注「GMV（下单口径 v1.0）」，结果与支付口径不同 |
| GMV 最近 30 天 | 用族默认支付口径，确认环节标注「支付口径」 |
| GMV 有哪几种口径 | 召回支付/下单/消费三种口径的公式与适用场景，请你选择 |
| 活跃用户的客单价最近 30 天 | 标注「活跃口径 v2.0（状态=active）」 |

### 边界与 ETL
| 提问 | 预期表现 |
|------|---------|
| 查 dwd_ord_pay_di 表里 pay_amt 大于 100 的 | 物理表/字段名被校验器拒绝，agent 修正或反问，不硬写物理名 |
| 帮我算一下「客户满意度」 | 未注册指标，列出候选或反问，不编造 |
| 帮我新建一个按周汇总的用户复购率指标 | 走 ETL 路径 D：查重 → 生成 DDL（遵守命名规范）→ 调度（预留提示）→ 审批 |

> 每条提问都会经过：规划（plan-routing）→ OAG 理解 → MQL 校验 → **确认环节** → 确定性翻译 → 口径标注回答。

## 更新与卸载

### 更新（升级到新版）
```bash
./install.sh update
```
自动完成：`git pull` 拉取最新代码 → 更新依赖 → **重建样例库**（schema 可能变化）→ 更新 DSH 配置
（patch 块中的 backend 路径、预设覆盖更新，旧预设自动备份为 `data-agent.bak-<时间戳>`）→ 跑评测门禁。
完成后**重启 DSH** 生效。

### 卸载（移除 DSH 集成与数据文件，源码保留）
```bash
./install.sh uninstall
```
自动完成：移除 `cordis.patch.yml` 中的 data-agent 块（不碰其他插件配置）→ 删除「数据助理」预设 →
删除样例库与虚拟环境。**源码（backend/、ontology 等仓库文件）保留**。
如需彻底移除 `dsh-mcp-client` 依赖（仅当无其他插件使用）：
`cd ~/.dsh/profiles/web && pnpm remove @deepseek-ai/dsh-mcp-client`

> 三个命令均**幂等**：重复执行安全；卸载只删除本工具创建的内容，不误删用户数据。

## 能力



| 能力 | 说明 |
|------|------|
| 先规划后执行 | Plan 四类意图分类（取数/元数据咨询/新建建模/其他操作）→ 确定性路径选择：取数 A（缺指标/维度→询问是否新建→走 D）、元数据检索、新建 D、其他直接调工具；取数禁止自我发挥 |
| 元数据检索 | `metadata_search`：查表（层/字段/粒度/预聚合）、指标口径（公式/过滤/版本/族）、加工逻辑（血缘）、就绪时间；基于 Ontology+样例库实现，可替换为真实元数据接口 |
| 分层数仓模拟 | 15 张表：DWD × 4、DWS × 4、ADS × 3、DIM × 4；统一 `dt`（'YYYYMMDD'）分区 |
| 三层口径一致 | 样例库的 DWS/ADS 由 DWD 聚合生成（模拟真实 ETL），同指标跨表结果一致 |
| 多表关联聚合 | 事实表 × 维度表自动 JOIN（JOIN 键来自 Ontology relations，零猜测） |
| 多指标 | 同域单表多列 + 跨域 CTE+FULL JOIN 对齐；无维度自动跨分区聚合（总和单行） |
| 指标族（多口径） | 同族多口径（pay/order/consume GMV）三级识别：精确命中→族默认→召回确认；回答标注口径名 |
| 业务黑话归一 | glossary 词典 + term_normalize 确定性归一（poi→门店、goods→产品）；回答回译用户用词 |
| 表选择优化 | 预聚合表优先；维度不覆盖自动回落明细表；有行级权限自动排除无权限列的表 |
| MQL 用户确认 | `mql_explain` 展示口径/维度/过滤/时间并签发 confirm_token；`semantic_translate` **必填 confirm_token**（工具层强制，MQL 变更需重新确认），防跳过确认直接翻译 |
| 配置外置 | 全部运行时配置走 `backend/.env`（`DATA_AGENT_*` 前缀，见 `.env.example`）：介质/方言/远程连接串/令牌 TTL/日志轮转参数 |
| 检索索引 | `ontology_search` 走加载期构建的倒排索引（名称/别名/展示名），O(1) 精确命中 + 前缀兜底，替代全量线性扫描 |
| 默认 t-1 | 未识别时间参数 → 默认查昨天，回答标注 |
| ETL（路径 D） | `ddl_generate` / `etl_generate` 已实现（Spark SQL，Hive 风格）；调度 `scheduler_submit` 预留待接平台 MCP |
| 建模流程 | 路径 D 按 modeling-workflow 规范阶段化执行（需求分析→方案→DDL/注册→ETL→测试→上线→调度→SLA/DQC），每阶段先产出逻辑报告（需求识别表/方案书模板）→ 用户确认 → 才真实执行；缺失信息多轮澄清不臆造 |
| 查询令牌 | `semantic_translate` 签发 query_token（绑定 SQL、短 TTL），`execute_sql` 必须携带校验——禁止绕过翻译引擎执行裸 SQL（行级权限/表选择/口径过滤不可被绕过） |
| 审计日志 | 所有 MCP 工具调用落 `backend/logs/audit.jsonl`（调用方/动作/入参摘要/耗时/结果规模），轮转清理不无限增长 |
| 运行日志 | `backend/logs/runtime.jsonl` 记录启动自检/连接/异常等运行态，jsonl + 轮转；启动自检（ontology/介质/日志目录）fail-fast，`health_check` 工具返回引擎状态 |
| 安全边界 | MQL 物理渗入检测（精确物理名集合）；只读执行器（禁写、行数上限）；查询令牌强制翻译→执行绑定 |
| 主题可替换 | 引擎零主题耦合：时间维度名/分区列名由 `ontology/config.yaml` 配置 |
| 评测门禁 | Golden Dataset 回归，通过率 ≥ 90% 才放行（CI 已配置） |
| 方言状态 | 查询执行：SQLite 已实现已测试（样例库）；MySQL/Doris/Hive/SparkSQL 为远程方言（翻译已映射，`dialect_verified` 由快照测试覆盖，执行需在 `backend/.env` 配 `DATA_AGENT_DSN_*` 接入驱动）。**DDL/ETL 演示统一 Spark SQL（Hive 风格）** |

## 架构

```
DSH 侧（配置，零代码）                Python 侧（引擎）
┌──────────────────────────┐   MCP    ┌──────────────────────────────┐
│ 数据助理 preset           │ ──────▶ │ mcp_servers/server.py        │
│  persona + 9 个 skills    │  stdio   │  ├ ontology_search/traverse  │
│  dsh-mcp-client           │          │  ├ mql_validate/explain     │
└──────────────────────────┘          │  ├ semantic_translate（翻译） │
                                      │  ├ execute_sql（只读执行）    │
                                      │  ├ ddl_generate / etl_generate（Spark SQL）
                                       │  └ scheduler_submit（预留）  │
                                      └──────────────────────────────┘
```

```
backend/
├── seed/          样例数据生成器（schema.sql + seed.py，固定种子）
├── builder/       本体半自动构建器（表结构 → Ontology YAML 骨架）
├── ontology/      Ontology（objects/functions/relations/glossary/config；order 为示例主题）
├── core/          确定性引擎（loader / validator / translator / executor /
│                  ddl_gen / etl_gen / query_token / confirm_token / audit /
│                  runtime_log / startup_check / config）
├── mcp_servers/   FastMCP 入口（12 个工具）
├── tests/         引擎单元测试（P0 安全 / P1 方言 / P2 可观测 / P4 工程化 / 路径 D）
└── eval/          Golden Dataset + 评测门禁
dsh-side/          setup_dsh.py + 「数据助理」预设模板 + 9 个 skills
```

## 二次开发

### 换主题域（order 只是示例）
引擎零主题耦合，换主题 = 替换 `backend/ontology/` 四份文件，代码不动：
1. `config.yaml`：改 `time_dimension`（时间维度属性名）与 `partition_column`（分区列名）；
2. `objects.yaml` / `functions.yaml` / `relations.yaml`：填新主题的对象/指标/关系；
3. 替换样例数据：新写 `seed/`（或用 `builder/build_ontology.py` 从真实库生成骨架后人工补全）；
4. 跑评测门禁。
> 内置 order 交易主题（下单/支付/消费/退款 + 产品/门店/区域/用户）是「可运行示例」，替换后即为你的领域。

### 换真实数仓
1. 配置连接：复制 `backend/.env.example` 为 `backend/.env`，填
   `DATA_AGENT_DSN_<方言>`（mysql/doris/hive/sparksql 均内置驱动接入骨架；
   SQL 不变，方言由翻译引擎按 dialect 生成）；
2. 装驱动：`uv pip install --python backend/.venv/bin/python pymysql`（mysql/doris）
   或 `pyhive thrift sasl`（hive/sparksql）；
3. 生成本体骨架：`uv run --project backend python -m builder.build_ontology --sqlite <库>`；
4. 🔴 人工补全：对象 `description`、指标 `formula`/口径/版本、relations `join_key` 核对；
5. 填 Golden Dataset 并跑门禁：`uv run --project backend python -m eval.eval`；
6. 方言翻译回归：`uv run --project backend python -m unittest tests.test_dialects`。

### 接入 ETL 平台能力（路径 D）
- `ddl_generate` 已实现（本地，遵守 `warehouse-standards` 命名规范）；
- `scheduler_submit` 为**预留占位**：接入公司调度平台 MCP（mcp-scheduler）后，在
  `mcp_servers/server.py` 的 `scheduler_submit` 里补充核心逻辑（任务依赖/周期/告警/幂等键）；
- ETL 代码模板与沙箱建表执行随平台能力逐步补齐。

### 加指标
编辑 `backend/ontology/functions.yaml`（formula 只允许 SUM/COUNT/AVG/MAX/MIN/DISTINCT + 属性名），
并在 `objects.yaml` 对应事实表的 `pre_aggregated` 里声明预聚合列（如有）。

### 加表/对象
编辑 `objects.yaml`（source_tables 声明粒度/权威/joinable/available_dims/perm_column）+
`relations.yaml`（JOIN 键）→ 跑评测门禁。

### 行级权限
翻译引擎按 `user.region_ids` 注入 `F.region_id IN (...)`（步骤 0）。真实接入时：
从你的权限系统查出当前用户的可见区域 id，由会话上下文填入 `user` 参数即可。

### 多指标（MQL v1.1）
`metrics` 列表已完整支持：
- **同域多指标**（如 GMV + 客单价 + 支付笔数）→ 单表多列（全预聚合 或 全明细公式）；
- **跨域多指标**（如 GMV + 退款金额，分属 Payment / Refund）→ CTE + FULL OUTER JOIN 按共同维度对齐；无维度时返回单行标量（CROSS JOIN）；
- 无维度查询自动跨分区聚合（返回总和单行，而非逐日多行）。

## 文档

- [方案设计（修订版 v2.0）](DataAgent落地技术方案-修订版v2.0.md)
- [落地指南（Python 版）](DSH实现DataAgent落地指南-Python版.md)
- [代码解读报告（当前代码）](DataAgent代码解读报告.md)

## License

MIT
