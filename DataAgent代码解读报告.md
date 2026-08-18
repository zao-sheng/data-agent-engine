# Data Agent 引擎 · 代码解读报告（Check 用）

> 对象：`data-agent-engine/`（commit `a7ff819`，6 次提交）
> 目的：逐模块解读实现，核对是否与《Data Agent 落地技术方案（修订版 v2.0）》一致，并标注简化点/预留点/风险点，供你 check。
> 结论先行：**核心链路（OAG→MQL→确定性翻译→执行→口径标注）已闭环且评测 19/19 通过**；多指标、行级权限、表选择、默认 t-1、多表 JOIN、主题无关均已落地；ETL 调度为预留占位。

---

## 0. 仓库总览

```
data-agent-engine/
├── backend/                       # Python 引擎（100% 纯 Python，零 JS）
│   ├── seed/          schema.sql + seed.py          # 15 表分层样例数据生成
│   ├── builder/       build_ontology.py             # 表结构 → Ontology 骨架（半自动）
│   ├── ontology/      objects/functions/relations/config.yaml   # Ontology 数据（order 示例主题）
│   ├── core/          ontology_loader / mql_validator / translator / executor
│   ├── mcp_servers/   server.py                     # FastMCP，8 个工具
│   └── eval/          golden_dataset.jsonl + eval.py  # 19 条金标准 + 门禁
├── dsh-side/          setup_dsh.py + data-agent preset + 8 个 SKILL.md
├── install.sh         # 一键安装
├── README.md          # 面向使用者
└── .github/workflows/eval.yml   # CI 评测门禁
```

**分层设计（理解与执行分离的物理体现）**：
- **确定性层**（`core/` + `mcp_servers/`）：零 LLM，纯规则，可单测、可评测门禁。
- **智能层**（DSH 侧 persona + skills）：LLM 只做「理解」（OAG 实体识别、MQL 生成、路径规划）。
- **知识层**（`ontology/`）：YAML 数据，OAG 检索锚点 + 翻译映射依据，二者共用。

---

## 1. 端到端数据流（一次取数查询）

```
用户提问
  │  DSH agent（data-agent 预设，persona 约束）
  ▼
① plan-routing skill   意图分类(L1-L4) + 路径选择(A/C/B/D)
  ▼
② 路径 A: oag-retrieval skill
     ontology_search → 实体/指标定位
     ontology_traverse → 关系图扩展（join_key/required_filter）
  ▼
③ mql-authoring skill → 生成 MQL
     mql_validate → 校验（物理渗入检测 = 安全边界）
  ▼
④ mql_explain → 中文确认信息 → ask_user_question 用户确认  ← 新增闸门
  ▼
⑤ semantic_translate → 确定性翻译（表选择/多表 JOIN/权限注入/默认 t-1）
  ▼
⑥ execute_sql → 只读执行 → 结果集
  ▼
⑦ 口径标注回答（指标版本/公式/必要过滤/数据区间）
```

**关键点**：步骤 ②③④ 是 LLM 参与的「理解」；步骤 ⑤⑥ 是零 LLM 的「执行」。二者通过 **MQL** 这一中间表示解耦——LLM 不接触物理表/字段，翻译引擎不接触自然语言。

---

## 2. Ontology 数据层（`backend/ontology/`）

四份 YAML，是 OAG 与翻译引擎**共用的唯一事实源**。

### 2.1 `config.yaml`（主题无关配置）
```yaml
time_dimension: order_date   # 时间维度业务属性名
partition_column: dt         # 物理分区列名
```
作用：把「时间维度名」「分区列名」从代码中抽出，**换主题零代码改动**（引擎读这两个配置）。

### 2.2 `objects.yaml`（业务对象 + 物理映射）
每个对象含：`properties`（业务属性）、`required_filters`（必要过滤）、`source_tables`（物理映射）。
`source_tables` 的关键字段（翻译引擎表选择的依据）：
| 字段 | 语义 | 例子 |
|------|------|------|
| `layer` | ODS/DWD/DWS/ADS/DIM | DWD |
| `authority` | gold/silver/bronze | gold |
| `joinable` | 能否 JOIN 维度表扩展（仅明细表 true） | dwd 明细 true |
| `perm_column` | 行级权限列（无则不能做权限过滤） | region_id |
| `granularities` | 表支持的聚合粒度 | [day,week,month,...] |
| `available_dims` | 表直接可用的业务属性 | [channel] |
| `field_mapping` | 业务属性→物理列 | pay_amount→pay_amt |
| `pre_aggregated` | 指标名→已预聚合的列 | gmv→gmv_amt |

### 2.3 `functions.yaml`（指标口径）
`formula`（只允许白名单函数+属性名）、`owner`（指标归属对象，决定事实表候选）、`required_filters`、`do_not`（防幻觉反面说明）、`version`。

### 2.4 `relations.yaml`（关系图 = JOIN 键唯一来源）
```yaml
- {source: Payment, target: Store, type: fulfills, join_key: store_id, cardinality: "N:1"}
```
**翻译引擎的 JOIN 键 100% 来自这里，永不猜测**（方案 §7.4 幻觉防控核心）。

---

## 3. 核心引擎逐模块解读

### 3.1 `core/ontology_loader.py`（118 行）——本体加载与索引

**职责**：读 4 份 YAML，构建翻译引擎与校验器共用的索引。

关键实现：
- `property_owner`：业务属性 → 归属对象（维度/过滤字段合法性校验 + JOIN 目标判定）。
- `edges`：关系图的邻接表（`source → [(target, join_key)]`）。
- `join_path(start, target)`：**BFS 最短路径**，返回 hop 序列 `[(src, dst, join_key)]`——多表 JOIN 链的构建依据（如 Payment→Store→Region 二跳）。
- `resolve_version(obj_name)`：**版本三级规则**（单版本直取 / default_version / 返回 None 需反问，绝不静默选最新）——对应方案 §6.2 Step 2。
- `physical_names`：物理表名 + 物理列名集合（**减去与业务指标/属性同名的条目**），供 MQL 物理渗入检测用（精确匹配，避免误伤如 `order_user_cnt` 指标名）。
- `time_dim` / `partition_col`：从 config.yaml 读，实现主题无关。

### 3.2 `core/mql_validator.py`（85 行）——MQL 校验 = 安全边界

6 项校验（顺序即优先级）：
1. **物理渗入检测（最优先）**：物理表名（`dwd_/dws_/...` 前缀正则）+ 物理列名（`physical_names` 精确 `\b` 匹配）→ 拒绝。
2. 指标存在性（metric 必须注册）。
3. 维度合法性（时间维度验粒度枚举，其余必须是注册属性）。
4. 过滤合法性（field 是注册属性 + operator 在枚举 + value 必填）。
5. 时间完整性（有时间维度缺 time_range → warning，翻译引擎按 t-1 兜底）。
6. 排序/限量合法性。

**设计要点**：校验失败返回结构化 `{ok, errors, warnings}`，由 agent 决定自动修正还是反问用户——不是直接报错给用户。

### 3.3 `core/translator.py`（437 行）——翻译引擎（核心，重点）

主入口 `translate(mql, user, dialect)`：捕获 `TranslateError`/`KeyError` 返回 `{"error": ...}`（MCP 工具友好），否则返回 `{"sql", "metadata"}`。

**流程 `_translate`**：
1. `_metric_items`：兼容 v1.0 单 `metric` 与 v1.1 `metrics` 列表。
2. **按 `owner` 分组指标**：同 owner → 单表多列；跨 owner → CTE 合并。
3. 单 owner 走 `_single_table_sql`，跨 owner 走 `_multi_table_sql`。

**表选择 `_select_table_multi`（核心决策）**：
```
候选表过滤：
  ├─ 粒度覆盖 _gran_ok（表粒度含请求粒度，或 day 粒度表可聚合成更大粒度）
  ├─ 权限：有 region_ids 时排除无 perm_column 的表（ads_ord_gmv_1d 被排除）
  ├─ 非预聚合指标必须落在明细表（joinable）
  └─ 维度覆盖：direct（available_dims 含全部 needed）或 joinable（明细表可 JOIN 维度表）
分桶：
  ├─ cands_preagg：预聚合表且直接覆盖全部指标 + 维度直连
  └─ cands_detail：明细表（公式编译）
选表：优先 cands_preagg（gold 优先 > ADS/DWS 优先），否则 cands_detail
```
这实现了方案 §7.1 的「指标×维度×粒度×时间范围→选表」决策，并正确处理了「月表答不了周查」「有权限时 ads 表不可用」等真实场景。

**公式编译 `_compile_formula`（白名单）**：
- 属性名 → `F.{物理列}`（查 field_mapping，不依赖全局注册）；
- 剩余标识符只允许 `SUM/COUNT/AVG/MAX/MIN/DISTINCT`，其余报「公式含非法标识符」；
- 再查物理表名正则兜底——**三重防护，杜绝公式注入**。

**JOIN 链 `_build_joins`**：对每个 needed 维度属性 → `join_path` 找路径 → 逐 hop 生成 `JOIN {dim表} {别名} ON {别名}.{join_key} = {父别名}.{join_key}`，同表只 JOIN 一次。

**多指标**：
- 同 owner 单表：`_metric_selects` 全预聚合列 或 全明细公式。
- 跨 owner：`_multi_table_sql` 生成 `WITH _m0 AS(...), _m1 AS(...) ...`，按共同维度 `FULL OUTER JOIN`（COALESCE 对齐维度），无维度则 `CROSS JOIN` 单行标量。

**时间 `_time_sql` / `_expr` / `_time_group_expr`**：
- 无 time_range → 默认 `dt = 昨天`（t-1）；
- 相对表达式（`-30d`/`today`/`last_month_start`...）→ SQL；
- 粒度表达式分 sqlite / doris 两套方言（sqlite 用 `substr` + 自定义 `iso_week()`，doris 用 `DATE_FORMAT`）。

**权限 `_permission_clause`（步骤 0）**：`user.region_ids` → `F.{perm_column} IN (...)`，注入 WHERE，不可跳过。

**一个已知边界**（代码注释 + README 已注明）：跨 owner ≥3 个时 FULL JOIN 以第一个子查询维度为锚（2 个 owner 为完整 COALESCE 对齐）。

### 3.4 `core/executor.py`——只读执行器

- 正则 `FORBIDDEN` 拦截非 SELECT/CTE 语句（insert/update/drop/...）；
- SQLite 以 `mode=ro` 只读打开（第二重拦截）；
- `_register_dialect_functions` 注册 `iso_week`（翻译引擎 week 粒度依赖）；
- 行数上限 `MAX_ROWS=1000`；
- **`_execute_remote` 为真实数仓扩展点**（覆盖此方法接入 Doris/Hive/ClickHouse，SQL 不变）。

### 3.5 `mcp_servers/server.py`（186 行）——MCP 工具面

8 个工具（`mcp__dataagent__*`）：
| 工具 | 层 | 用途 |
|------|----|------|
| ontology_search | OAG | 实体/指标/属性检索 |
| ontology_traverse | OAG | 关系图 BFS 扩展 |
| mql_validate | 安全 | MQL 校验 + 物理渗入检测 |
| mql_explain | 确认 | MQL → 中文确认信息（口径/维度/过滤/时间） |
| semantic_translate | 执行 | 确定性翻译 |
| execute_sql | 执行 | 只读执行 |
| ddl_generate | ETL | 对象 → 建表 DDL（遵守命名规范） |
| scheduler_submit | ETL | 调度（**预留占位**） |

---

## 4. 数据、构建与评测

### 4.1 `seed/`（15 表分层样例，dt 分区）

- 15 张表：DWD ×4（下单/支付/消费/退款明细）、DWS ×4（日汇总）、ADS ×3（GMV 日/月、用户日）、DIM ×4（产品/门店/区域/用户）。
- 所有事实表统一 `dt`（`'YYYYMMDD'`）分区；字段跨层冗余（region_id/store_id 在 dwd/dws/退款表重复），模拟真实数仓。
- **DWS/ADS 由 DWD 聚合生成**（`INSERT ... SELECT ... GROUP BY`），保证三层口径严格一致（实测明细 GMV = dws = ads）。
- 预构建 `store_id→region_id` 索引消除 O(n) 扫描；固定随机种子可复现；10% 无效单让 `is_valid=1` 过滤有真实意义。

### 4.2 `builder/build_ontology.py`（半自动本体构建）

从 SQLite `PRAGMA table_info` 推断：表分类（dwd/dws/ads→事实，dim→维度）、字段映射、required_filters（is_valid 等）、关系候选（`*_id`→同名 dim 表）、粒度（表名后缀）。生成 YAML 骨架后**人工补 description/公式/join_key**。真实库模式留 `--connect` 待接入元数据 API。

### 4.3 `eval/`（19 条金标准 + 门禁）

- 12 条基础（单指标各场景 + 负例）+ 7 条多指标（同域/跨域/无维度标量/按周）。
- 断言：校验通过/应拒绝、表选择正确、SQL 可执行、行数、结果列含全部指标、多表合并标记。
- `eval.py` 返回 0/1，通过率 ≥90% 才过（GitHub Actions CI 已配置）——**发布门禁**。

---

## 5. DSH 侧（preset + 8 skills + 安装）

### 5.1 `dsh-side/setup_dsh.py` + `install.sh`（一键接入）
- `setup_dsh.py`（幂等）：写 `cordis.patch.yml`（mcp-client 行）→ 补 profile 的 `dsh-mcp-client` 依赖 → 复制 preset 并替换 `{{BACKEND}}` 占位。
- `install.sh`：装 uv 环境 → 生成样例库（--sample）→ 调 setup_dsh → 提示重启。

### 5.2 `agent.cordis.yml` persona（智能层约束）
8 条硬性规则：先 plan-routing → 路径执行 → MQL 校验 → **mql_explain 用户确认** → 翻译执行 → 口径标注；默认 t-1；多版本反问；ETL 走 D 且先审批；不编造。

### 5.3 8 个 SKILL.md（路径闭环）
| Skill | 对应方案 |
|-------|---------|
| plan-routing | §4.3 Plan Agent 路径决策 |
| oag-retrieval | §6.2 OAG 五步 |
| mql-authoring | §5 MQL 规范 |
| path-a-query | §4.2 路径 A（含用户确认闸门） |
| path-c-skill | §10 Skill 直执行 |
| path-b-fallback | §3.2 路径 B 兜底 |
| path-d-etl | §4.3 路径 D ETL |
| warehouse-standards | 数仓分层/主题/命名规范（规范地址可替换） |

---

## 6. 与设计方案（修订版 v2.0）映射核对表

| 方案要点 | 实现位置 | 状态 |
|---------|---------|------|
| 理解与执行分离 | MQL 中间层 + core 零 LLM | ✅ 完全实现 |
| OAG 五步 | oag-retrieval skill + ontology_search/traverse | ✅（文档检索 Step4 留待接入） |
| MQL schema v1.1 + 校验 | validator + mql-authoring | ✅（含物理渗入检测） |
| 翻译引擎确定性（表选择/公式/方言） | translator | ✅ |
| 多表 JOIN 键不猜 | relations.yaml + join_path | ✅ |
| 多指标（v1.1） | translator 同域/跨域 | ✅ |
| 表选择（预聚合优先/回落明细/权限排除） | _select_table_multi | ✅ |
| 行级权限注入（步骤 0） | _permission_clause | ✅（region_ids 维度） |
| 默认 t-1 | _time_sql | ✅ |
| 版本解析三级规则 | resolve_version + persona | ✅（数据层实现，agent 调用） |
| Plan Agent 路径决策 | plan-routing skill | ✅（LLM 决策，非独立 Agent） |
| 五职能 Agent | 合并为「主 agent + skills + MCP 工具」 | ⚠️ 简化（v1 精简版，符合 §11.6 建议） |
| Skill 体系 | 8 个 SKILL.md | ✅ |
| MCP 集群 | dsh-mcp-client + 8 工具 | ⚠️ 平台 MCP 未接入（scheduler 预留） |
| ETL 路径 D | ddl_generate + scheduler_submit | ⚠️ DDL 已实现，调度/代码模板预留 |
| 反馈飞轮 | 未实现 | 🔲 待接入 dsh-message-feedback |
| 评测体系 | eval 19 条 + CI 门禁 | ✅ |
| 分阶段路线图 | README + install.sh | ✅（P0 样例跑通） |
| LangGraph 编排 | 未用，用 DSH 主循环 + skills | ⚠️ 设计决策（见 §7） |
| 沙箱 | executor 只读 + DSH 沙箱 | ✅ |
| Ontology 存储瘦身 | YAML 文件（未用 PG/图库） | ⚠️ 简化（P0 阶段，方案 §8.4 的起步态） |

---

## 7. 关键设计决策记录（为什么这样实现）

1. **不用 LangGraph，用 DSH 主循环 + skills + MCP 工具**：DSH 的 agent-loop 本身就是状态机，workflow/subagent 提供 fan-out。方案 §11.6 的「v1 精简」落地为「主 agent + 确定性工具」，LLM 调用从 5 次链路降到 2~3 次。
2. **五职能 Agent 合并为 skills**：Intent/Plan/SQL/Quality/Respond 由 persona + 对应 skill 承担，Monitor 由 task-board 承担（未接入）。符合「最少职能满足确定性，需要再拆」原则。
3. **Ontology 用 YAML 文件而非图库/关系库**：P0 起步态（方案 §8.4 存储瘦身），对象 <200 用内存加载足够，换真实规模再上图库。
4. **时间维度名/分区列名可配置**：主题无关的关键，换领域零代码。
5. **物理渗入检测用「精确物理名集合」而非「后缀正则」**：避免误伤 `order_user_cnt` 这类与物理列同名的业务指标名。

---

## 8. Check 结论

### 符合预期（✅）
核心闭环完整、评测 19/19、确定性引擎零 LLM、多指标/多表 JOIN/行级权限/默认 t-1/主题无关全部落地且经实测。

### 简化点（⚠️，建议知悉）
1. **多 Agent 未物理拆分**——按方案 §11.6 精简为「主 agent + skills」，符合设计但若后续要独立调优某职能需再拆。
2. **OAG 文档检索（Step 4）未实现**——依赖 FAQ/历史案例库，P1 接入。
3. **跨 owner ≥3 指标 FULL JOIN 以首表为锚**——2 指标完整对齐，3+ 指标的维度对齐可能不完整（真实场景极少）。
4. **行级权限只支持 region_ids 一维**——换 store_id 等需在 translator 扩展 `perm_column` 的多值支持。

### 预留/待办（🔲）
1. **ETL 调度**：`scheduler_submit` 占位，待接公司 mcp-scheduler。
2. **ETL 代码模板**：DDL 已生成，Spark/Flink 代码模板未做。
3. **反馈飞轮**：dsh-message-feedback 三态 → Golden Dataset 自动入库未接。
4. **真实数仓执行**：`_execute_remote` 待实现（SQLite 默认）。
5. **多指标跨域 ≥3 owner 严格对齐**：可选补强。

### 风险点
1. **`_build_joins` 依赖 relations 完整性**——join_key 错误会导致 JOIN 结果错误（不报错）。需在真实数仓接入前人工核对 relations.yaml。
2. **公式编译的字段替换基于 field_mapping**——field_mapping 缺列会 `KeyError`（有兜底返回 error，但需留意）。
3. **SQLite 方言的 week 粒度依赖 `iso_week` 自定义函数**——切真实数仓后 week 粒度需确认对应引擎函数。
