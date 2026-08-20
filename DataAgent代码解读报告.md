# Data Agent 引擎 · 代码解读报告 v3

> ⚠️ 本报告为阶段性代码解读快照，仓库结构/统计随迭代更新——以 README、`backend/tests/` 实际文件与测试结果为准。
> 基准（原始）：commit `ac83dc7`（59 个文件，24 次提交）· 评测门禁 29/29（100%，阈值 ≥90%）
> 最新：评测门禁 29/29 · 引擎单测 171 项（14 文件）· MCP 工具 19 个
> 目的：基于**当前代码**逐层解读技术架构、各层作用、安全模型、端到端执行过程与关键技术细节，供整体核对。

---

## 0. 仓库快照

```
data-agent-engine/  (59 files, 24 commits)
├── backend/                        # Python 引擎（100% Python，零 JS）
│   ├── core/        18 模块（含 intent.py 意图识别，行数见 §3 各小节标题）
│   ├── mcp_servers/  server.py     # FastMCP，19 个工具
│   ├── ontology/     config · objects · functions · relations · glossary（5 YAML）
│   ├── seed/         schema.sql（15 表）· seed.py（固定种子生成器）
│   ├── builder/      build_ontology.py（表结构→本体骨架）
│   ├── eval/         golden_dataset.jsonl（29 条：21 翻译执行 + 8 意图分类）· eval.py（门禁）
│   └── tests/        14 个文件 171 项测试（详见 §7）
├── dsh-side/         setup_dsh.py · test_setup_dsh.py
│   └── agent-presets/data-agent/   preset + persona + 8 skills
├── install.sh · README.md · LICENSE(MIT) · backend/.env.example
└── .github/workflows/eval.yml      # CI：评测门禁 + 单测 + DSH 侧回归
```

---

## 1. 技术架构

### 1.1 三层架构

```
┌───────────────────────────────────────────────────────────────┐
│ 智能层（LLM 只做理解）—— DSH 侧：persona（9 条硬性规则）          │
│   8 skills：plan-routing · oag-retrieval · mql-authoring ·     │
│   query-metric · explore-fallback · modeling-etl ·             │
│   modeling-workflow · warehouse-standards                      │
└──────────────┬────────────────────────────────────────────────┘
               │ MCP（stdio，dsh-mcp-client）
┌──────────────▼────────────────────────────────────────────────┐
│ 确定性层（零 LLM）—— Python 引擎（14 模块 ≈2000 行，详见 §3）    │
│   server.py（14 工具·审计包装·双令牌·启动自检）                   │
│   ontology_store(存储抽象) · ontology_loader · mql_validator · │
│   executor · metadata · ddl/etl/modeling_plan · token ×2 ·     │
│   audit · runtime_log · startup_check · config                 │
└──────────────┬────────────────────────────────────────────────┘
               │ 读取
┌──────────────▼────────────────────────────────────────────────┐
│ 知识层（数据）—— backend/ontology/（order 为示例主题，可替换）    │
│   config · objects · functions · relations（JOIN 键唯一来源）·  │
│   glossary（黑话词典）                                          │
└────────────────────────────────────────────────────────────────┘
```

### 1.2 分层职责

| 层 | 职责 | 技术 | 关键约束 |
|----|------|------|---------|
| 智能层 | 自然语言→MQL、意图路径规划、口径确认、回答生成 | DSH agent + skills（LLM） | 只输出业务语义；物理层不可见；输出受校验器与双令牌约束 |
| 确定性层 | MQL 校验、确认/翻译/执行、令牌校验、审计、Ontology 访问 | Python（纯规则） | 零 LLM；可单测；安全边界在工具层强制（不依赖提示词） |
| 知识层 | 业务对象/指标/关系/黑话/配置 | YAML（数据） | 翻译与校验的唯一事实源；换主题零代码 |

### 1.3 设计决策

1. **理解与执行分离**：MQL 是唯一契约——LLM 侧产出、引擎侧消费，两侧互不越界（LLM 不碰物理层，引擎不碰自然语言）。
2. **Ontology 单事实源**：OAG（检索）、校验器（合法性）、翻译引擎（映射/JOIN）、确认环节（口径展示）、元数据检索五者读同一份 YAML，杜绝口径漂移。
3. **主题无关**：时间维度名/分区列名（config.yaml）、SQL 别名（动态生成）、物理名集合（从 Ontology 收集）全部数据驱动——order 只是示例。
4. **工具即接口**：引擎的一切能力通过 MCP 工具暴露，DSH 侧不 import Python 代码，耦合面 = 工具 schema。
5. **安全边界在工具层**：物理渗入检测、只读执行、权限注入、**双令牌（confirm_token/query_token）**、审计日志都在确定性层强制，不依赖提示词；**配置外置**：全部 `DATA_AGENT_` 前缀（`backend/.env`，见 .env.example），换主题/换环境零代码。

---

## 2. 知识层：Ontology 详解（5 份 YAML）

### 2.1 `config.yaml` —— 主题无关配置
`time_dimension: order_date`（时间维度【业务属性名】，MQL 的 dimensions 用它）+ `partition_column: dt`（物理分区列名 'YYYYMMDD'）。
被 loader 读取为 `onto.time_dim` / `onto.partition_col`，translator / validator / ddl_gen / etl_gen 全部引用这两个字段——**换主题只改这里**。

### 2.2 `objects.yaml` —— 业务对象 + 物理映射
8 个对象：Order / Payment / Consume / Refund（事实域）+ Product / Store / Region / ActiveUser（DIM 维度）。

`source_tables` 字段语义（**表选择决策的数据基础**）：

| 字段 | 语义 | 例子 |
|------|------|------|
| `layer` / `authority` | 分层（ODS/DWD/DWS/ADS/DIM）/ 权威等级（gold>silver） | DWD / gold |
| `joinable` | 能否 JOIN 维度表扩展维度（仅明细表 true） | dwd_ord_pay_di: true |
| `perm_column` | 行级权限列（无则权限过滤时被排除） | region_id |
| `granularities` | 支持聚合到的粒度 | [day,week,month,quarter,year] |
| `available_dims` | 表**直接可用**的业务属性 | [channel] |
| `field_mapping` | 业务属性 → 物理列 | pay_amount → pay_amt |
| `pre_aggregated` | {指标名: 已预聚合列} | gmv → gmv_amt |

### 2.3 `functions.yaml` —— 指标口径（含指标族）
9 个指标，其中 `gmv` 族含 3 个变体（支付/下单/消费口径）：

| 指标 | 族/口径 | 公式 | owner |
|------|---------|------|-------|
| gmv | gmv/支付口径（**族默认**） | SUM(pay_amount) | Payment |
| order_gmv | gmv/下单口径 | SUM(order_amount) | Order |
| consume_gmv | gmv/消费口径 | SUM(consume_amount) | Consume |
| avg_order_amount | — | SUM(pay_amount)/COUNT(DISTINCT order_id) | Payment |
| pay_count / order_count / order_user_cnt / refund_amount / consume_amount | — | … | … |

每个指标还有 `required_filters`、`do_not`（防幻觉反面说明）、`version`（口径版本）、`supported_dimensions/granularities`——口径全部以 Ontology 为准，翻译/确认/ETL 均引用同一份。

### 2.4 `relations.yaml` —— 关系图（JOIN 键唯一来源）
15 条边：事实对象→维度对象（join_key）+ 维度间（Store→Region）；同一对象对可有多条边（Payment→Product / Refund→Payment）。**翻译引擎的 JOIN 键 100% 来自这里，永不猜测**——幻觉防控的核心。

### 2.5 `glossary.yaml` —— 业务黑话词典
23 条：poi/店铺/店面→Store、goods/商品→Product、成交额/销售额→gmv、实付金额→pay_amount 等。`canonical` 必须指向已注册对象/指标/属性；loader 合并对象/属性 `aliases` 构建倒排索引；**只收录「真黑话」（标准名自身不替换）**。

### 2.6 各层对 Ontology 的消费方式

| 消费者 | 用 Ontology 做什么 |
|--------|-------------------|
| OAG（ontology_search/traverse） | 实体定位（倒排索引）、关系扩展 |
| mql_validator | 指标/属性存在性、物理名集合 |
| translator | field_mapping、pre_aggregated、relations（JOIN）、时间配置 |
| mql_explain / metric_disambiguate / term_normalize | 指标公式/版本/口径名/属性含义；指标族变体与默认；黑话归一 |
| metadata（metadata_search） | 表/口径/血缘/就绪时间 |
| ddl_gen / etl_gen / modeling_plan | 对象属性→DDL、指标公式→ETL、变更清单→配对方案 |

---

## 3. 确定性层：core 引擎（17 模块 ≈2700 行）

### 3.1 `ontology_loader.py`（234 行）—— 索引工厂

**构建 9 类索引**（`__init__`）：

| 索引 | 类型 | 用途 |
|------|------|------|
| `objects` / `functions` | dict | 对象/指标定义查询 |
| `property_owner` | 属性→对象 | 维度/过滤合法性 + JOIN 目标判定 |
| `edges` | 关系邻接表 | BFS 关系图遍历 |
| `families` | 族→{variants, default} | 指标族识别 |
| `term_index` / `physical_names` | 黑话→{canonical,display,type} / 物理表名+列名集合 | 术语归一（只收真黑话）/ 物理渗入检测（减业务名避免误伤） |
| `dim_aliases` / `search_index` | 维度对象→SQL 别名 / 归一化 token→命中行 | 多表 JOIN（动态生成，仅 DIM 层，避开 F，冲突加序号）/ ontology_search 倒排检索（O(1)，精确+前缀兜底） |

**核心方法**：
- `join_path`：BFS 最短路径 → `[(hop_src, hop_dst, join_key)]`——JOIN 链构建依据（如 Payment→Store→Region 两跳）。
- `resolve_version`：**版本三级规则**——单版本直取 / `default_version` / None（调用方必须反问，绝不静默选最新）。
- `normalize_terms`：**最长匹配**黑话归一 → `(normalized_text, mappings)`，目标=展示名；`search`：倒排索引（精确 token + 前缀兜底，去重排序）。

### 3.2 `mql_validator.py`（85 行）—— 校验 = 安全边界

6 项校验（按优先级）：
1. **物理渗入检测**：物理表名（`dwd_/dws_/...` 前缀正则，兜底强信号）+ 物理列名（`physical_names` 精确 `\b` 匹配）→ 拒绝。精确集合解决了「后缀正则误伤 `order_user_cnt` 指标名」的问题。
2. 指标存在性（must be registered，未注册给候选清单）。
3. 维度合法性（时间维度验粒度枚举 day/week/month/quarter/year；其余必须是注册属性）；过滤合法性（field/operator 枚举/value 必填）。
4. 时间完整性（有时间维度缺 time_range → warning，引擎按 t-1 兜底）；排序/限量合法性。

### 3.3 `translator.py`（512 行）—— 翻译引擎（核心）

**方法地图**：
```
translate()            入口：捕获 TranslateError/KeyError → {"error"}
├── _metric_items()    兼容 v1.0 metric / v1.1 metrics 列表
├── _translate()       按 owner 分组 → 单表 or 多表；组装 metadata（含 dialect/dialect_verified）
│   ├── _single_table_sql()  同 owner：_select_table_multi（⭐表选择/分桶评分）
│   │   ├── _metric_selects()/_dim_expr()  预聚合列或公式编译 / 维度列→粒度表达式
│   │   └── _map_required()/_filter_sql()/_permission_clause()/_time_sql()  required_filter/过滤/权限/dt 分区
│   └── _multi_table_sql() 跨 owner：CTE + FULL OUTER JOIN / CROSS JOIN
├── _build_joins()      JOIN 链（relations BFS）
├── _compile_formula()  公式白名单编译
└── _order_limit()      排序/限量
```

**metadata 输出**（可审计性）：`metrics / metric_versions / fact_table / tables_used / pre_aggregated / injected_filters / time_range / granularity / dialect / dialect_verified / multi_table`。

### 3.4 `executor.py`（180 行）—— 只读执行

- 双重拦截：`FORBIDDEN` 正则（insert/update/delete/drop/alter/create/attach/detach/pragma/vacuum/reindex）+ SQLite `mode=ro` URI 只读打开；行数上限 1000（`fetchmany(MAX_ROWS+1)` 判截断）；注册 `iso_week` 方言函数（SQLite 无法解析 `YYYYMMDD` 紧凑格式，week 粒度表达式依赖它）。
- **方言与介质解耦**：`dialect=="sqlite"` → SQLite；非 sqlite + `.db` 介质 → 明确报错（介质不匹配）；否则 `_execute_remote`——mysql/doris 走 pymysql（Doris 兼容 MySQL 协议）、hive/sparksql 走 pyhive（Thrift），DSN 来自 `DATA_AGENT_DSN_<方言>`；缺 DSN / 缺驱动 / 不支持的 scheme 均给出可操作的错误提示。

### 3.5 `config.py`（74 行）+ `startup_check.py`（101 行）—— 配置外置 + 启动自检

- **config**：集中读取配置，优先级「进程环境变量 > backend/.env > 默认值」，全部 `DATA_AGENT_` 前缀（`DB / ONTOLOGY / LOG_DIR / LOG_LEVEL / DIALECT / TOKEN_TTL / TOKEN_MAX / CONFIRM_TOKEN_* / AUDIT_* / REMOTE_TIMEOUT / DSN_<方言>`）。
- **startup_check**：server 启动即校验——Ontology 可解析（对象/指标非空）、数据介质可达（sqlite 只读打开 / 远程 DSN 已配置）、日志目录可写；返回结构化 CheckResult，必需项失败即 fail-fast 暴露配置问题；`startup_status` 供 health_check 输出状态快照。

### 3.6 `metadata.py`（173 行）—— 元数据检索

覆盖元数据咨询四类诉求（表 / 指标口径 / 血缘 / 就绪时间）：
- `table_info`：物理表 → 层/中文层名/归属对象/粒度/joinable/authority/perm_column/字段（含物理列与描述）/预聚合指标；`find_tables` / `find_metrics`：关键字模糊检索。
- `lineage`：汇总/应用表 → 来源 DWD 明细 + 加工说明（样例库内置 `_LINEAGE` 推导；DWD 明细返回「无聚合加工」）；`readiness`：表就绪时间（样例库约定 t-1 / T+1）。
- `search`：统一入口，返回 `{tables, metrics, lineage, readiness, note}`，无命中给候选清单；**可替换**——接入真实元数据平台后替换 `MetadataService.search` 数据源即可，返回结构不变。

### 3.7 `ddl_gen.py`（83 行）+ `etl_gen.py`（137 行）+ `modeling_plan.py`（139 行）—— 路径 D 生成器

- **ddl_gen**：从 Ontology 对象生成 **Spark SQL DDL 草稿（Hive 风格）**——类型映射（STRING/DECIMAL(18,2)/DATE/INT/BIGINT）；事实表统一 `PARTITIONED BY (dt STRING)`（分区列不进普通列）；维表无分区；列级/表级 COMMENT 保留口径。命名规范 `{layer}_{domain}_{subject}_{后缀}`（明细 `_di` / 日汇总 `_1d` / 月汇总 `_1m` / 维表无后缀）。
- **etl_gen**：生成 **Spark SQL INSERT OVERWRITE 聚合脚本**——源=目标对象同 owner 的 DWD 明细；指标公式复用 Ontology formula（白名单编译 + field_mapping 落物理列）；required_filters 自动注入 WHERE；目标按 dt 分区；跨域指标自动 CTE 合并（与翻译引擎多指标策略一致）。
- **modeling_plan**：变更清单（create / add_field / modify_logic / register）→ **配对 {DDL, ETL} 列表**——新增/加字段事实表必须 DDL 与 ETL 成对（N 表 = N 对，`summary.paired` 校验）；modify_logic 仅 ETL；register 仅注册项；DIM 维表无需 ETL；每项带 `editable` 字段供逐项编辑；数量不一致/缺 DWD 源等明确报错，不静默产出不完整方案。

### 3.8 `query_token.py`（75 行）+ `confirm_token.py`（80 行）+ `audit.py`（62 行）+ `runtime_log.py`（64 行）—— 安全与可观测
详见第 5 章「安全模型」。

### 3.9 `intent.py`（208 行）—— 意图识别（确定性预分类）
plan-routing Step1 的结构化信号：实体识别（Ontology 指标/对象/黑话归一）+ 词表信号（取数/元数据结构词/建模动词/存量修改动词）→ 按序判定四类意图（取数 A / 元数据 / 建模 D / 存量操作）→ 输出 `{intent, confidence, path, evidence[], mixed[], metrics_missing}`。零 LLM、可单测、可入 eval 门禁；LLM 基于证据裁决，unclear/low 为预分类未覆盖（非错误），由 LLM 结合归一化文本与会话上下文兜底裁决，确实无法确定才向用户澄清（并给出倾向判断）。

### 3.10 `ontology_store.py`（≈260 行）—— 本体存储抽象（YAML / SQLite / Supabase）
把「本体数据从哪来」与「Ontology 索引构建」解耦——换存储只实现一个新 Store，Ontology 与全部调用方零改动：
- **接口**：`OntologyStore.load() -> OntologyData`（原始 YAML 结构）；`OntologyData`（objects/functions/relations/glossary/config 五段）
- **YamlOntologyStore**（默认）：读 `backend/ontology/*.yaml`，clone 即跑、零依赖
- **SqliteOntologyStore**：读编译产物（`ontology_compile` YAML→SQLite，5 表 + meta 记录 schema_version/compiled_at/source_commit），发布时预构建、运行只读
- **SupabaseOntologyStore / SupabaseOntologyWriter**（阶段 2，多人编辑真源）：走 PostgREST HTTP（轻依赖）；读 = 全量拉取还原 YAML 形状；写 = upsert（Prefer merge-duplicates）+ revision 乐观锁（`update_object_if_revision` 冲突返回 False）；建表脚本 `backend/supabase/schema.sql`；`ontology_export` 导出 YAML 评审副本
- **create_store 工厂**：`yaml`（默认）/ `sqlite` / `supabase`，按 `DATA_AGENT_ONTOLOGY_STORE` 切换
- 一致性保障：YAML↔SQLite↔Supabase 三种 store 构建的 Ontology 行为等价（单测覆盖数据一致性 + 索引行为一致）

---

## 4. 工具面：`mcp_servers/server.py`（18 个工具）

server 启动：`setup_runtime_logger` → 构建 Ontology/Validator/Translator/Executor/MetadataService → `run_startup_checks`（fail-fast）→ 初始化双令牌存储 → `setup_audit_logger` → FastMCP("data-agent")。所有工具经 `@_audit_tool` 包装（保留签名供 schema 生成；记录耗时/成败/入参摘要/结果规模，审计失败不影响主流程）。

| 工具 | 层 | 输入→输出 | 说明 |
|------|----|----------|------|
| health_check | 可观测 | —→状态快照 | 引擎状态（ontology 规模/介质/自检结果），排障入口 |
| ontology_search | OAG | 文本→命中清单 | 倒排索引 O(1)，对象/指标/属性，只返回注册内容 |
| ontology_traverse | OAG | 对象名→关系图 | BFS，含 join_key/required_filter/可达对象 |
| term_normalize | OAG | 文本→{normalized, mappings} | 黑话归一（Step 0），回答按 mappings 回译 |
| intent_classify | 规划 | 文本→{intent, path, confidence, evidence, mixed} | 意图预分类（plan-routing Step1）；unclear/low 由 LLM 结合上下文兜底，仍不确定才澄清 |
| metric_disambiguate | 识别 | 文本→{status, exact/family/candidates} | 指标族三级识别 |
| mql_validate | 安全 | MQL→{ok, errors, warnings} | 物理渗入检测 + schema 校验 |
| mql_explain | 确认 | MQL→中文确认信息 + **confirm_token** | 口径/版本/维度含义/过滤/时间展示；用户执行前必调 |
| semantic_translate | 执行 | MQL+confirm_token+user+dialect→SQL+metadata+**query_token** | 确定性翻译；confirm_token 必填（绑定 MQL 指纹） |
| execute_sql | 执行 | SQL+query_token→结果集 | 只读+限行 1000；query_token 必填（绑定 SQL 原文） |
| metadata_search | 元数据 | 查询词→{tables, metrics, lineage, readiness} | 元数据咨询意图；可替换真实接口 |
| ddl_generate | 路径 D | 对象+层→Spark SQL DDL | 遵守分层命名规范；人工 Review+审批 |
| etl_generate | 路径 D | 对象+层+指标/维度→Spark SQL ETL | INSERT OVERWRITE 聚合；人工 Review+审批 |
| modeling_plan | 路径 D | 变更清单→配对 DDL+ETL | summary.paired 强制校验；返回 editable 字段 |
| ontology_register | 路径 D | kind+entry→写本体存储 | 双通道（yaml / supabase，乐观锁）；注册后自动重载本体 |
| ontology_reload | 运维 | 无参→重读本体 | 运行时刷新（多人编辑后当前会话读到最新） |
| scheduler_submit | 路径 D | task_spec→预留提示 | **预留占位**（待接平台 mcp-scheduler） |

方言参数：`dialect: sqlite（默认，已实现已测试）/ mysql / doris / hive / sparksql（远程方言仅翻译，需在 backend/.env 配置 DATA_AGENT_DSN_<方言> 并接入 _execute_remote 后执行）`。

---

## 5. 安全模型（双令牌 + 审计日志）

### 5.1 查询令牌 query_token（P0）—— 防绕过翻译执行裸 SQL

问题：`execute_sql` 若接受任意 SQL，调用方可绕过翻译引擎（semantic_translate）直接查询物理表——行级权限、表选择、required_filter 全部形同虚设。

方案（`core/query_token.py`，进程内内存存储，无落盘）：
- `semantic_translate` 翻译成功 → `_token_store.issue(tr["sql"])` 签发 query_token（绑定 **SQL 原文** + TTL）；`execute_sql` 必须携带 → `verify`：令牌不存在 / 已过期 / **SQL 与签发时不一致** 均拒绝。
- 安全属性：短 TTL（默认 300s，`DATA_AGENT_TOKEN_TTL`）+ 惰性 GC；容量上限 500（超出淘汰最旧）；单令牌可多次执行（幂等只读）；重启即失效。

### 5.2 确认令牌 confirm_token（P4）—— 防跳过用户确认

问题：persona/skill 要求查询先 mql_explain 展示口径再请用户确认，但工具层不强制——agent 可能跳过确认直接翻译。

方案（`core/confirm_token.py`）：`mql_explain(mql)` 成功 → 签发 confirm_token（绑定 **MQL 指纹**：metrics/dimensions/filters/time_range 的确定性 SHA-256 摘要 + TTL）；`semantic_translate` 必须携带 → 指纹校验：**MQL 变更（指纹变化）必须重新 explain**，否则拒绝。TTL 默认 600s，容量 200。

分工闭环：`confirm_token` 管「MQL → 翻译」（防跳过确认）；`query_token` 管「SQL → 执行」（防绕过翻译）——两段式闸门把安全从 prompt 约束升级为**工具层强制**。

### 5.3 审计日志 audit.jsonl + 运行日志 runtime.jsonl

- **audit（P0-3，`core/audit.py`）**：所有工具调用经 `@_audit_tool` 落一行 JSON 到 `backend/logs/audit.jsonl`——字段 `ts / tool / action / outcome / elapsed_ms / args 摘要 / result 摘要`；**记录「查询意图」与「结果规模」，不记录敏感原始数据**（SQL 只记 SHA-256 指纹前缀、结果集只记行数/截断/列名）；`RotatingFileHandler` 轮转（单文件 10MB × 7 份，`DATA_AGENT_AUDIT_*` 可配）；写失败静默降级，绝不影响取数主链路。
- **runtime（P2）**：运行态日志 `runtime.jsonl`（启动自检/警告/异常等事件），面向排障，同一套轮转策略；`health_check` 用 `startup_status` 输出自检快照作为排障入口。

### 5.4 其他安全边界（工具层强制，不依赖提示词）

| 边界 | 实现位置 | 说明 |
|------|---------|------|
| 物理渗入检测 | mql_validator + translator 公式 | MQL/公式中出现物理表名/列名即拒绝 |
| 只读执行 + 行数上限 | executor FORBIDDEN 正则 + SQLite mode=ro + MAX_ROWS=1000 | 禁写禁 DDL（远程方言同样拦截），fetchmany(1001) 判截断 |
| 行级权限 | translator `_permission_clause` | user.region_ids → F.perm_column IN(...)，mandatory 注入；无 perm_column 的表在选表阶段即排除 |
| 公式白名单 | `_compile_formula` | 仅白名单函数 + 属性名 + 运算符 |

---

## 6. 智能层：DSH 侧

### 6.1 persona（`agent.cordis.yml`，9 条硬性规则）

1. 先加载 plan-routing 做规划：意图分类（取数/元数据咨询/新建建模/其他操作）→ 确定性路径选择 → 加载对应 skill 执行；**取数必须走规范路径，不允许自我发挥**。
2. 取数路径（A）：OAG 五步 → 生成 MQL → mql_validate 校验 → **mql_explain 生成确认信息（含 confirm_token）+ 请用户确认** → semantic_translate（必须携带 confirm_token；MQL 变更需重新 explain）→ execute_sql（必须携带 query_token）。MQL 出现物理表名/字段名是 schema 错误，修正后重验。
3. MQL 确认环节（L2 及以上必须）：中文展示口径/版本/维度/过滤/时间，ask_user_question 请求确认；用户修改则改 MQL，拒绝则不执行。
4. 回答格式（口径标注，强制）：「指标 X（口径 vX.X：公式；必要过滤；数据区间；数据截至）结果：...」。
5. 未给时间参数默认 t-1（昨天），回答标注。
6. 多版本指标先按 default_version，无法确定必须反问；多口径指标族：明示口径用变体、只说族名用默认并标注、问差异则召回全部变体。
7. 先 term_normalize 术语归一，回答用词跟随用户原话（回译）。
8. 建模/ETL 走路径 D：按 modeling-workflow「需求分析→方案→落地(配对DDL+ETL)→测试→上线→调度→SLA/DQC」阶段化；每阶段先产出完整逻辑报告（模板、每项带编号），走**清单确认协议**（一次 ask_user_question 整体确认，禁止拆多次单问选择题），确认后才调用 modeling_plan / scheduler_submit；信息缺失多轮澄清，不得臆造口径；**演示引擎统一 Spark SQL**。
9. 不编造 Ontology 之外的指标/属性/表；不输出推测口径。

### 6.2 意图路径四类分类（plan-routing）

| 意图 | 判定（metadata_search / ontology_search 辅助） | 路径 |
|------|---------|------|
| **取数** | 要**查数据/看数值**（"昨天 GMV 多少"） | A → query-metric；**缺指标/维度 → 询问是否新建**（是走 D / 否结束，禁止近似替代） |
| **元数据咨询** | 找表/指标维度/加工逻辑/口径/就绪时间/血缘 | metadata_search（必要时 ontology_search/traverse 补充解读） |
| **新建/建模** | 新建表/ETL/指标维度/本体（含"缺指标维度要求新建"） | D → modeling-etl → modeling-workflow（进入后第一步先输出完整需求识别清单再整体确认） |
| **其他操作** | 修改 ETL/调度参数/调整口径等存量变更 | 直接调用对应 MCP（etl_generate / scheduler_submit 等），遵守工具约束与审批 |

### 6.3 8 个 skills（路径闭环）

| Skill | 对应路径 | 职责 |
|-------|---------|------|
| plan-routing | 规划 | 意图四类分类 + 路径选择（A/元数据/D/其他）+ 缺指标维度分支 |
| query-metric | 路径 A | 取数全流程（前置查重 → OAG → MQL → 确认闸门 → 翻译 → 执行 → 口径标注） |
| oag-retrieval | 路径 A | OAG 五步 + Step 0 术语归一 + 回译 |
| mql-authoring | 路径 A | MQL v1.1 规范 + 指标族三级识别 + 多指标语法 |
| explore-fallback | 受限兜底 | 探索性长尾（四条件全满足才允许：缺指标且拒绝新建且明确要求且显式同意；标记"非注册口径"） |
| modeling-etl | 路径 D 入口 | 加载 modeling-workflow + warehouse-standards，阶段化编排，确认后调工具 |
| modeling-workflow | 路径 D | 阶段 0-7 全流程 + 清单确认协议 + 模板 |
| warehouse-standards | 治理 | 分层/分主题/命名规范（公司规范地址可替换） |

### 6.4 路径 D 阶段化流程（modeling-workflow）

| 阶段 | 产出物 | 确认点 |
|------|--------|--------|
| 0 需求分析 | 需求识别提取表（可编辑草稿，每项带**来源**：需求文档/用户input/待确认） | 清单确认协议：一次整体确认 |
| 1 方案设计 | 建模方案书（查重 + 现状对照 + 变更评估，可编辑） | 清单确认协议 |
| 2/3 方案落地 | **配对 DDL+ETL 列表**（modeling_plan，`summary.paired` 必须 True）+ 本体注册项 | N 表=N 对，一次整体确认 |
| 4/5 测试与上线 | 测试报告（Golden+DQC）；发布记录（变更清单/DDL 执行/回滚方案） | 测试结论/上线确认 |
| 6 调度 | 调度配置（cron/依赖/告警/幂等键；scheduler_submit 预留） | 调度参数确认 |
| 7 SLA/DQC | 治理配置（产出时间点/空值率/主键唯一性/波动阈值；预留接数据质量平台） | 治理规则确认 |

**清单确认协议（全局统一）**：① 先输出完整清单（markdown，每项带编号）一次展示全部 → ② 再调用**一次** `ask_user_question`：「以上清单请整体确认，或指出要修改的项（编号+新内容）」，选项 `[整体确认 (Recommended), 我要修改（回复编号+新值）, 增补信息]` → ③ 用户回复修改 → 更新清单 → 重新输出全文 → 再整体确认，直到确认 → ④ **禁止**把同一清单拆成多次单问选择题、禁止每次只展示一项。核心原则：**每个阶段先产出逻辑报告 → 用户整体确认 → 才调用 MCP 真实执行**。

### 6.5 setup_dsh.py / install.sh

- **setup_dsh.py** 三模式（幂等）：`install`（补 dsh-mcp-client 依赖 + upsert cordis.patch.yml 块 + 复制 preset 替换 `{{BACKEND}}`）/ `update`（备份覆盖预设）/ `uninstall`。patch 块用 `- insert:` 包裹（顶层裸条目会被 loader 静默跳过——历史踩坑）；stdio spawn 用绝对路径 + 显式 `cwd` 指向 backend。
- **install.sh** 子命令：`install/--sample` / `--real`（配 .env + builder 生成本体）/ `update`（git pull → 重建样例库 → 更新 DSH 配置 → 评测门禁）/ `uninstall`；统一 `uv run --project backend` 单一入口。

---

## 7. 数据与评测

- **seed**：15 表（DWD×4/DWS×4/ADS×3/DIM×4），统一 `dt` 分区，字段跨层冗余；**DWS/ADS 由 DWD 用 SQL 聚合生成**（三层口径一致，评测可交叉验证）；固定种子 42 可复现；10% 无效单让过滤有意义；近 90 天数据。
- **builder**：PRAGMA 读表结构 → 推断对象/映射/关系候选/粒度 → YAML 骨架（人工补 description/指标 formula/join_key 核对）；换主题时辅助生成本体。
- **eval**：29 条金标准——21 条翻译执行（基础取数 / 多指标同域+跨域 / 指标族变体 / 行级权限 / 负例，断言：校验/表选择/可执行/行数/结果列/多表合并）+ **8 条意图分类**（expect_intent/mixed/metrics_missing，plan 层门禁）；通过率 ≥90% 门禁（当前 29/29 100%）；GitHub Actions CI（seed 重建 → eval → 引擎单测 → DSH 侧回归）。
- **tests**：14 个文件 171 项全部通过（test_security 10 / test_dialects 5 / test_observability 6 / test_metadata 12 / test_modeling 15 / test_modeling_plan 13 / test_intent 19 / test_entry_validator 20 / test_explore 10 / test_ontology_store 18 / test_supabase_store 18 / test_ontology_writer 13 / test_ontology_sync 4 / **test_table_metadata 8**）。

---

## 8. 端到端执行过程详解

### 8.1 完整链路（一次取数查询，含双令牌）

```
① 用户提问 ──▶ DSH agent（数据助理预设）
② plan-routing：意图分类（四类）→ 路径选择（A/元数据/D/其他）
③ oag-retrieval：Step0 term_normalize 归一 → Step1 ontology_search 定位 →
     Step2 版本/口径确认 → Step3 ontology_traverse 关系图：join_key/required_filter）
④ mql-authoring：生成 MQL → mql_validate 校验
⑤ mql_explain → 中文确认信息 + confirm_token → ask_user_question 用户确认
⑥ semantic_translate(mql, confirm_token=…)（确定性翻译：权限注入/表选择/JOIN/时间/五方言）
   → 翻译成功签发 query_token（绑定 SQL）
⑦ execute_sql(sql, query_token=…)（只读，限行 1000）→ 结果集 → ⑧ 口径标注回答（指标名+口径+版本+公式+过滤+数据区间）
```

### 8.2 逐步责任矩阵

| 步骤 | 执行者 | 调用 | 返回 | 约束 |
|------|--------|------|------|------|
| ② 规划 | LLM | skill | 意图/路径 | skill 指令，取数禁止自我发挥 |
| ③ 理解 | LLM+工具 | term_normalize/ontology_* | 归一文本/实体/关系 | 工具供给受限 |
| ④ 生成 MQL | LLM | mql_validate | MQL/校验结果 | 校验器兜底 |
| ⑤ 确认 | LLM+用户+引擎 | mql_explain/ask_user | 确认信息/confirm_token/确认结果 | persona 强制 + confirm_token 工具层强制 |
| ⑥ 翻译 | 引擎 | semantic_translate | SQL+metadata+query_token | 零 LLM + confirm_token 校验 |
| ⑦ 执行 | 引擎 | execute_sql | 结果集 | 只读+限行+query_token 校验 |
| ⑧ 回答 | LLM | — | 口径标注文本 | persona 规则 |

### 8.3 实例走查 1：单指标 + 多表 JOIN（真实数据）

用户：**「华东区数码类产品的 GMV，按门店城市拆分，最近 30 天」**（golden #3）

```
②③ plan/理解：路径 A；无黑话；实体 Product(数码)/Region(华东)/Store(城市)；指标 gmv
   关系扩展：Payment--product_id-->Product / --store_id-->Store / --region_id-->Region
④ MQL：{metrics:[gmv], dimensions:[city], filters:[product_type=数码, region_name=华东], time_range:-30d~today} → mql_validate ok
⑤ mql_explain → 「GMV（支付口径 v1.0：SUM(pay_amount)，过滤 is_valid=1）按门店城市分组，
   过滤产品类型=数码/区域=华东，时间 -30d~today」+ confirm_token → 用户确认
⑥ semantic_translate(confirm_token=…)：
   表选择 → ads/dws 无 city/product_type/region_name → 排除；dwd_ord_pay_di（joinable）→ 命中
   JOIN 链 → dim_product P / dim_store S / dim_region R；权限 → F.region_id IN(...)；
   时间 → F.dt BETWEEN strftime('%Y%m%d',date('now','-30 days')) AND strftime('%Y%m%d','now')
   产出 SQL：SELECT (SUM(F.pay_amt)) AS gmv, S.city AS city FROM dwd_ord_pay_di F
     JOIN dim_product P ON P.product_id=F.product_id JOIN dim_store S ON S.store_id=F.store_id
     JOIN dim_region R ON R.region_id=F.region_id
     WHERE F.is_valid=1 AND P.product_type='数码' AND R.region_name='华东'
       AND F.dt >= ... AND F.dt <= ... GROUP BY S.city ORDER BY gmv DESC
   → 翻译成功，签发 query_token
⑦ execute_sql(sql, query_token=…) → 示例 [{上海: 37550.67}, {南京: 31598.79}, {杭州: 22926.65}, {苏州: 39312.36}]
⑧ 回答 → 「GMV（支付口径 v1.0，过滤 is_valid=1，数据区间 -30d~today）上海 37,550.67 元 / …」
```

### 8.4 实例走查 2：跨域多指标（CTE 合并）

用户：**「最近 30 天 GMV 和退款金额，按门店类型」**（golden #15）

```
MQL：{metrics:[gmv, refund_amount], dimensions:[store_type], time_range:-30d~today}
翻译引擎：按 owner 分组 → {Payment:[gmv], Refund:[refund_amount]} → 跨 owner，各生成子查询：
    WITH _m0 AS (SELECT SUM(F.pay_amt) AS gmv, S.store_type FROM dwd_ord_pay_di F
                 JOIN dim_store S ... GROUP BY S.store_type),
         _m1 AS (SELECT SUM(F.refund_amt) AS refund_amount, S.store_type FROM dwd_ord_refund_di F
                 JOIN dim_store S ... GROUP BY S.store_type)
    SELECT COALESCE(_m0.store_type, _m1.store_type), _m0.gmv, _m1.refund_amount
    FROM (_m0 FULL OUTER JOIN _m1 ON _m0.store_type = _m1.store_type)
执行 → 示例 [{加盟: gmv=966258.85, refund=24493.56}, {直营: gmv=1238015.37, refund=22621.03}]
```

### 8.5 实例走查 3：黑话 + 指标族（理解层前置处理）

用户：**「华东区 poi 的 goods 销售额，按门店类型拆」**

```
③ Step0 term_normalize → "华东区 门店 的 产品 gmv，按门店类型拆"（mappings: poi→门店, goods→产品, 销售额→gmv）
   metric_disambiguate("销售额") → 黑话命中 → exact gmv（支付口径，族默认）
④ MQL → ⑤ mql_explain 确认（标注支付口径）+ confirm_token → ⑥⑦ 执行
⑧ 回答用词跟随用户：用「门店」回译（而非标准名 Store）
```

### 8.6 实例走查 4：元数据咨询

用户：**「ads_ord_gmv_1d 这张表是怎么来的？GMV 怎么算？」**

```
② plan：意图=元数据咨询（要元信息，不要数值）→ metadata_search
③ metadata_search("ads_ord_gmv_1d") →
    tables: [{layer: ADS, layer_cn: 应用层, granularities: [day],
              pre_aggregated: [gmv, order_count, refund_amount], fields: [...]}]
    metrics: [{name: gmv, formula: SUM(pay_amount), required_filters: [is_valid=1],
               version: v1.0, family: gmv, variant_label: 支付口径}]
    lineage: {source: dwd_ord_pay_di, logic: "按 dt 聚合：GMV（SUM(pay_amt)，过滤 is_valid=1）"}；readiness: {t-1（昨日）}
④ 按 mapping 解读回答；不产生数值结果
```

### 8.7 ETL 路径 D 走查（阶段化 + 配对）

用户：**「帮我新建一个按周汇总的用户复购率指标」**

```
plan → 意图=新建/建模 → 路径 D（modeling-etl → modeling-workflow）
阶段0 需求分析：ontology_search 查重（复购率未注册）→ 输出需求识别提取表（编号+来源）
     → 一次整体确认（缺口径/粒度先澄清，不臆造）
阶段1 方案设计：现状对照（完全新增）→ 建模方案书（变更清单编号）→ 整体确认
阶段2/3 落地：modeling_plan([{type: create, obj_name: Order, layer: ADS,
              subject: repurchase, metrics: [order_count]}])
     → 配对 {DDL: CREATE TABLE ... ads_ord_repurchase_1d ... PARTITIONED BY (dt STRING),
                 ETL: INSERT OVERWRITE TABLE ads_ord_repurchase_1d PARTITION (dt)
                      SELECT F.dt, ... FROM dwd_ord_order_di F WHERE F.is_valid=1 GROUP BY F.dt}
     → summary.paired=True（N 表 = N 对）→ 输出 {DDL,ETL} 对清单 → 一次整体确认
     → register 项：编辑 backend/ontology/ 注册对象/指标 → 展示注册项清单 → 整体确认
阶段4-7：测试（Golden+DQC）→ 上线（发布/回滚）→ 调度（scheduler_submit 预留）→
   SLA/DQC（治理配置登记，预留）——每阶段先出报告 → 清单确认协议 → 确认后执行；
   全程审计日志留痕
```

### 8.8 异常链路

| 场景 | 行为 |
|------|------|
| MQL 物理渗入（用户说 dwd_xxx/pay_amt） | validator 拒绝 → agent 修正为业务属性或反问 |
| 指标未注册（"客户满意度"） | validator 拒绝 → 候选列表 → 澄清 |
| 表选择失败 / 有行级权限但只有 ads 表 | translator 返回 error → 降级链（映射缺失→治理建议；探索性→受限兜底）；表选择排除无 perm_column 的表 → 回落 dws/dwd |
| 取数缺指标/维度 | 告知缺失 + 询问是否新建（是走 D / 否结束）；**禁止近似替代**；拒绝新建且显式同意 → explore-fallback（受限，标记"非注册口径"） |
| confirm_token 缺失/过期/MQL 变更 | semantic_translate 拒绝 → 重新 mql_explain |
| query_token 缺失/过期/SQL 不符 | execute_sql 拒绝 → 重新 semantic_translate |
| 远程方言 + sqlite 介质 / 缺驱动 / 未配置 DSN | 明确报「介质不匹配」/ 给出安装指引（uv pip install pymysql / pyhive thrift sasl）/ 指出配置项名 |
| 多版本对象无默认 | resolve_version 返回 None → agent 反问用户 |
| 建模配对数量不一致（2 DDL / 1 ETL） | modeling_plan summary.paired=False + errors → 先解决再继续，禁止落地不完整方案 |

---

## 9. 关键技术细节

### 9.1 表选择决策算法（`_select_table_multi`）

```
候选过滤：粒度覆盖（gran∈表粒度 或 day 可聚合成更大粒度） × 权限（perm_rids 须有
         perm_column） × 指标覆盖（非预聚合指标必须落明细表） × 维度覆盖
         （direct：available_dims 全含；或 joinable：明细表可 JOIN 维度表）
分桶：cands_preagg（预聚合全指标覆盖+维度直连） vs cands_detail（明细表公式）；选表：preagg
优先（gold > ADS/DWS 层序），否则 detail；失败 → 降级链——体现「月表答不了周查」「有权限时 ads 表不可用」。
```

### 9.2 JOIN 链构建、别名与公式白名单
- `_build_joins`：对每个 needed 维度属性 → `join_path` BFS → 逐 hop `JOIN {dim表} {别名} ON {别名}.{join_key} = {父别名}.{join_key}`，同表只 JOIN 一次；事实表固定 `F`，维度别名由 loader **动态生成**（仅 DIM 层、首字母大写、避开 F、冲突加序号）。
- `_compile_formula` 三重防护：① 属性名 → `F.{物理列}`（查所选表 field_mapping，不依赖全局注册）；② 移除 `F.xxx` 后仅允许 `SUM/COUNT/AVG/MAX/MIN/DISTINCT`；③ 物理表名正则兜底。杜绝公式注入与幻觉列。

### 9.3 多指标合并
- 同 owner：单表多列（全预聚合 或 全明细公式）；跨 owner：CTE（`WITH _m0 AS(...), _m1 AS(...)`）+ 按共同维度 `FULL OUTER JOIN`（COALESCE 对齐），无维度 `CROSS JOIN` 单行标量。
- **无维度时预聚合列包 `SUM()`** 跨分区聚合（否则返回逐分区多行，语义错误）。

### 9.4 时间系统（五方言）
- 缺省 → `dt = 昨天`（t-1）；相对表达式（`-30d`/`today`/`last_month_start`/`last_month_end`）→ SQL；具体日期 `YYYY-MM-DD`/`YYYYMMDD` → 字面量。
- 时间表达式（TIME_EXPRS）与粒度分组（GRAN_EXPRS）按方言注册：sqlite 用 `strftime`/`substr` + 自定义 `iso_week()`（SQLite 无法解析 `YYYYMMDD` 紧凑格式）；mysql/doris 用 `DATE_FORMAT`/`DATE_SUB(CURDATE(),...)`（周 `%x-W%v`）；hive 用 `FROM_UNIXTIME(UNIX_TIMESTAMP()-n*86400)`；sparksql 用 `DATE_FORMAT(CURRENT_DATE)`/`DATE_SUB(CURRENT_DATE, n)`（hive/sparksql 周用 `WEEKOFYEAR()`）。

### 9.5 权限注入（步骤 0）与物理渗入检测
- 权限：`user.region_ids` → `F.{perm_column} IN (...)`，mandatory 注入 WHERE，**不可跳过**；无 perm_column 的表在选表阶段即被排除。
- 渗入检测：物理表名 → 前缀正则（`dwd_/dws_/ads_/dim_/ods_`）；物理列名 → `physical_names` 精确集合（**减业务名**避免误伤 `order_user_cnt`），比后缀正则更精确、主题无关。

### 9.6 指标族三级识别（`metric_disambiguate`）
黑话命中（glossary type=metric）→ 口径词命中（"下单GMV"→variant_label）→ 精确指标名 → 族匹配（variants+差异+default，status=family/exact/both）→ 候选。skill 侧：明示口径用变体；只说族名用默认+标注；问差异则召回确认。

### 9.7 术语归一与回译（`normalize_terms`）
最长匹配（词长降序）；只收录"真黑话"（标准名自身不替换）；归一目标 = 展示名（对象用中文 display_name）；回译：mappings 保留 raw→display，回答用词跟随用户。

### 9.8 方言系统与 DIALECT_VERIFIED
```python
DIALECT_VERIFIED = {"sqlite": True, "mysql": False, "doris": False,
                    "hive": False, "sparksql": False}
```
- metadata 恒带 `dialect_verified`；sqlite 已实现且由 eval 真实执行覆盖。
- 远程四方言**翻译已映射**（TIME_EXPRS/GRAN_EXPRS 全表）并由 test_dialects 快照测试锁定（5 个测试覆盖各用例在五方言下的关键特征），但 `dialect_verified=False`——执行需 `DATA_AGENT_DSN_<方言>` 接入驱动（pymysql / pyhive thrift）后逐个点亮。
- executor 方言与介质解耦：远程方言 + .db 介质明确报错；缺 DSN/缺驱动/不支持的 scheme 均有可操作提示。**消除"远程方言已支持"的误导**。

### 9.9 版本解析（`resolve_version`）
单版本直取 / `default_version` / None（调用方反问）。与指标族（并行口径）互补：前者是时间演进，后者是并行口径。

### 9.10 双令牌实现细节
- confirm_token：指纹 = metrics/dimensions/filters/time_range 的规范 JSON → SHA-256 前 16 位，不匹配即要求重新 explain；query_token：绑定 SQL 原文（`entry.sql != sql` 即拒绝），`secrets.token_hex(16)` 不可预测，审计只记 `fingerprint(sql)` 前缀。
- 两者均为**进程内内存存储**：TTL 300/600s、惰性 GC、容量上限（500/200）、重启即失效；单令牌可重复使用（幂等只读）。

---

## 10. 与设计方案（修订版 v2.0）映射核对

| 方案要点 | 实现 | 状态 |
|---------|------|------|
| 理解与执行分离 | MQL 契约 + core 零 LLM | ✅ |
| OAG 五步 | skills + 4 个 OAG 工具（含 Step0 归一） | ✅（文档检索 Step4 待接） |
| MQL v1.1 校验 | validator | ✅ |
| 翻译引擎确定性 | translator（5 方言映射） | ✅ |
| 多表 JOIN 键不猜 | relations + join_path | ✅ |
| 多指标 | 同域/跨域 CTE | ✅ |
| 行级权限 | perm_column 注入 | ✅ |
| 默认 t-1 | time_sql | ✅ |
| 版本三级规则 | resolve_version | ✅ |
| 多口径指标族 | family + metric_disambiguate | ✅ |
| 业务黑话 | glossary + term_normalize + 回译 | ✅ |
| 双令牌（防绕过翻译/防跳过确认） | query_token + execute_sql 校验 / confirm_token + semantic_translate 校验 | ✅（P0/P4） |
| 审计日志 + 运行日志 + 启动自检 | audit.jsonl / runtime.jsonl（轮转）+ @_audit_tool + health_check | ✅（P0/P2） |
| 四类意图路径 | plan-routing（取数/元数据/新建/其他） | ✅ |
| 元数据检索 | metadata_search（可替换真实接口） | ✅ |
| 路径 D 阶段化 | modeling-workflow 阶段 0-7 + 清单确认协议 | ✅ |
| 配对 DDL+ETL | modeling_plan（summary.paired） | ✅ |
| 演示引擎 Spark SQL | ddl_gen / etl_gen（Hive 风格） | ✅ |
| ETL 调度 | scheduler_submit | ⚠️ 预留 |
| Skill 体系 | 8 skills | ✅ |
| 评测门禁 | 21 条 + CI + 58 项单测 | ✅ |
| 五职能 Agent / 反馈飞轮 | 主 agent + skills / 未接 | ⚠️ 精简 / 🔲 |
| 真实数仓执行 | _execute_remote + 四方言驱动 | 🔲 接入 DSN 后点亮 |

---

## 11. 已知边界、预留与风险

**简化点（⚠️）**
1. 多 Agent 未物理拆分（主 agent + skills，符合 §11.6 精简建议）。
2. OAG 文档检索（Step 4）未实现（依赖 FAQ 库，P1 接入）。
3. 跨 owner ≥3 指标 FULL JOIN 以首表维度为锚（2 指标完整对齐）；行级权限只支持 `region_ids` 一维（多权限维度需扩展 perm_column 多值）。
4. metadata_search 基于 Ontology + 样例 schema 推导（血缘/就绪时间为样例语义），非真实元数据平台数据。

**预留（🔲）**
1. ETL 调度：`scheduler_submit` 占位，待接平台 mcp-scheduler（任务依赖/周期/告警/幂等键）；ETL 代码模板（Spark/Flink 生产化）。
2. 反馈飞轮（dsh-message-feedback → golden 自动入库）。
3. 真实数仓执行：`DATA_AGENT_DSN_<方言>` 接入后点亮 `dialect_verified`（mysql/doris 走 pymysql，hive/sparksql 走 pyhive Thrift）。
4. SLA/DQC 自动登记（待接数据质量平台）。

**风险点（⚠️）**
1. JOIN 键正确性完全依赖 `relations.yaml`——错误导致「结果错误但不报错」，真实数仓接入前必须人工核对；`field_mapping` 缺列 → `KeyError`（translate 有兜底返回 error，但需留意）。
2. week 粒度依赖 `iso_week` 方言函数（sqlite）/ `WEEKOFYEAR`（hive/sparksql）/ `%x-W%v`（mysql/doris）——切真实数仓需确认对应引擎函数。
3. 双令牌为进程内内存存储：进程重启后 agent 长会话中的旧令牌失效，需重新 explain/translate（交互成本，非安全缺陷）。
4. 样例库由 seed 以"今天"为锚生成，评测数值随日期变化（固定种子保证结构可复现）。
