# Data Agent 引擎 · 代码解读报告 v2

> 基准：commit `0db9cb2`（41 个文件，9 次提交）· 评测门禁 21/21
> 目的：基于**当前代码**逐层解读技术架构、各层作用、端到端执行过程与关键技术细节，供整体核对。

---

## 0. 仓库快照

```
data-agent-engine/  (41 files)
├── backend/                        # Python 引擎（100% Python，零 JS）
│   ├── core/         ontology_loader(189行) · mql_validator(86行)
│   │                translator(443行) · executor(84行) · __init__
│   ├── mcp_servers/  server.py     # FastMCP，10 个工具
│   ├── ontology/     config · objects · functions · relations · glossary（5 YAML）
│   ├── seed/         schema.sql（15 表）· seed.py（固定种子生成器）
│   ├── builder/      build_ontology.py（表结构→本体骨架）
│   └── eval/         golden_dataset.jsonl（21 条）· eval.py（门禁）
├── dsh-side/         setup_dsh.py · agent-presets/data-agent/（preset + 8 skills）
├── install.sh · README.md · LICENSE(MIT) · .gitignore
└── .github/workflows/eval.yml      # CI 评测门禁
```

---

## 1. 技术架构

### 1.1 三层架构

```
┌─────────────────────────────────────────────────────────────────┐
│ 智能层（LLM 只做理解）—— DSH 侧                                  │
│   persona（8 条硬性规则）                                        │
│   8 个 skills：plan-routing / oag-retrieval / mql-authoring /    │
│                path-a / path-b / path-c / path-d / warehouse-standards │
└───────────────┬─────────────────────────────────────────────────┘
                │ MCP（stdio，dsh-mcp-client）
┌───────────────▼─────────────────────────────────────────────────┐
│ 确定性层（零 LLM）—— Python 引擎                                │
│   mcp_servers/server.py（10 个工具，含 2 个 OAG / 2 个 ETL 预留） │
│   core/ontology_loader.py  → 索引与关系图（OAG 与翻译共用）       │
│   core/mql_validator.py    → 校验 + 物理渗入检测（安全边界）      │
│   core/translator.py       → MQL → SQL（表选择/JOIN/多指标/方言） │
│   core/executor.py         → 只读执行（SQLite 默认，远程扩展点）  │
└───────────────┬─────────────────────────────────────────────────┘
                │ 读取
┌───────────────▼─────────────────────────────────────────────────┐
│ 知识层（数据）—— backend/ontology/（order 为示例主题，可替换）    │
│   config.yaml（时间维度名/分区列名）· objects · functions ·       │
│   relations（JOIN 键唯一来源）· glossary（黑话词典）              │
└─────────────────────────────────────────────────────────────────┘
```

### 1.2 分层职责

| 层 | 职责 | 技术 | 关键约束 |
|----|------|------|---------|
| 智能层 | 自然语言→MQL、路径规划、口径确认、回答生成 | DSH agent + skills（LLM） | 只输出业务语义；物理层不可见；输出受校验器约束 |
| 确定性层 | MQL 校验、翻译、执行、Ontology 访问 | Python（纯规则） | 零 LLM；可单测；评测门禁覆盖 |
| 知识层 | 业务对象/指标/关系/黑话/配置 | YAML（数据） | 翻译与校验的唯一事实源；换主题零代码 |

### 1.3 设计决策

1. **理解与执行分离**：MQL 是唯一契约——LLM 侧产出、引擎侧消费，两侧互不越界（LLM 不碰物理层，引擎不碰自然语言）。
2. **Ontology 单事实源**：OAG（检索）、校验器（合法性）、翻译引擎（映射/JOIN）、确认环节（口径展示）四者读同一份 YAML，杜绝口径漂移。
3. **主题无关**：时间维度名/分区列名（config.yaml）、SQL 别名（动态生成）、物理名集合（从 Ontology 收集）全部数据驱动——order 只是示例。
4. **工具即接口**：引擎的一切能力通过 MCP 工具暴露，DSH 侧不 import Python 代码，耦合面 = 工具 schema。
5. **安全边界在工具层**：物理渗入检测、只读执行、权限注入都在确定性层强制，不依赖提示词。

---

## 2. 知识层：Ontology 详解（5 份 YAML）

### 2.1 `config.yaml` —— 主题无关配置
```yaml
time_dimension: order_date   # 时间维度【业务属性名】（MQL 的 dimensions 用它）
partition_column: dt         # 物理分区列名（'YYYYMMDD'）
```
被 loader 读取为 `onto.time_dim` / `onto.partition_col`，translator 与 validator 全部引用这两个字段——**换主题只改这里**。

### 2.2 `objects.yaml` —— 业务对象 + 物理映射
9 个对象：Order / Payment / Consume / Refund（事实域）+ Product / Store / Region / ActiveUser（DIM 维度）。

`source_tables` 字段语义（**表选择决策的数据基础**）：

| 字段 | 语义 | 例子 |
|------|------|------|
| `layer` | 分层（ODS/DWD/DWS/ADS/DIM） | DWD |
| `authority` | 权威等级（gold>silver） | gold |
| `joinable` | 能否 JOIN 维度表扩展维度（仅明细表 true） | dwd_ord_pay_di: true |
| `perm_column` | 行级权限列（无则权限过滤时被排除） | region_id |
| `granularities` | 支持聚合到的粒度 | [day,week,month,quarter,year] |
| `available_dims` | 表**直接可用**的业务属性 | [channel] |
| `field_mapping` | 业务属性 → 物理列 | pay_amount → pay_amt |
| `pre_aggregated` | {指标名: 已预聚合列} | gmv → gmv_amt |

### 2.3 `functions.yaml` —— 指标口径（含指标族）
9 个指标，其中 `gmv` 族含 3 个变体：

| 指标 | 族/口径 | 公式 | owner |
|------|---------|------|-------|
| gmv | gmv/支付口径（**族默认**） | SUM(pay_amount) | Payment |
| order_gmv | gmv/下单口径 | SUM(order_amount) | Order |
| consume_gmv | gmv/消费口径 | SUM(consume_amount) | Consume |
| avg_order_amount | — | SUM(pay_amount)/COUNT(DISTINCT order_id) | Payment |
| pay_count / order_count / order_user_cnt / refund_amount / consume_amount | — | … | … |

每个指标还有 `required_filters`、`do_not`（防幻觉反面说明）、`version`（口径版本）。

### 2.4 `relations.yaml` —— 关系图（JOIN 键唯一来源）
15 条边：事实对象→维度对象（join_key）+ 维度间（Store→Region）。
**翻译引擎的 JOIN 键 100% 来自这里，永不猜测**——方案 §7.4 幻觉防控的核心。

### 2.5 `glossary.yaml` —— 业务黑话词典
~25 条：poi/店铺/店面→Store、goods/商品→Product、成交额/销售额→gmv、实付金额→pay_amount 等。
`canonical` 必须指向已注册对象/指标/属性；由 loader 合并对象/属性的 `aliases` 构建倒排索引。

### 2.6 各层对 Ontology 的消费方式

| 消费者 | 用 Ontology 做什么 |
|--------|-------------------|
| OAG（ontology_search/traverse） | 实体定位、关系扩展 |
| mql_validator | 指标/属性存在性、物理名集合 |
| translator | field_mapping、pre_aggregated、relations（JOIN）、时间配置 |
| mql_explain | 指标公式/版本/口径名、属性含义 |
| metric_disambiguate | 指标族变体与默认 |
| term_normalize | 黑话归一 |
| ddl_generate | 对象属性→DDL |

---

## 3. 确定性层：core 引擎

### 3.1 `ontology_loader.py`（189 行）—— 索引工厂

**构建 6 类索引**（`__init__`）：

| 索引 | 类型 | 用途 |
|------|------|------|
| `objects` / `functions` | dict | 对象/指标定义查询 |
| `property_owner` | 属性→对象 | 维度/过滤合法性 + JOIN 目标判定 |
| `edges` | 关系邻接表 | BFS 关系图遍历 |
| `families` | 族→{variants, default} | 指标族识别 |
| `term_index` | 黑话→{canonical, display, type} | 术语归一（只收真黑话，标准名不替换） |
| `physical_names` | 物理表名+列名集合 | 物理渗入检测（减业务名避免误伤） |
| `dim_aliases` | 维度对象→SQL 别名 | 多表 JOIN（动态生成，仅 DIM 层，避开 F，冲突加序号） |

**核心方法**：
- `join_path(start, target)`：BFS 最短路径，返回 `[(hop_src, hop_dst, join_key)]`——JOIN 链构建依据（如 Payment→Store→Region 两跳）。
- `resolve_version(obj)`：**版本三级规则**——单版本直取 / `default_version` / 返回 None（调用方必须反问，绝不静默选最新）。
- `normalize_terms(text)`：**最长匹配**黑话归一，返回 `(normalized_text, mappings)`，归一目标为展示名（对象用中文 display_name），标准名自身不替换。

### 3.2 `mql_validator.py`（86 行）—— 校验 = 安全边界

6 项校验（按优先级）：
1. **物理渗入检测**：物理表名（`dwd_/dws_/...` 前缀正则，兜底强信号）+ 物理列名（`physical_names` 精确 `\b` 匹配）→ 拒绝。精确集合解决了「后缀正则误伤 `order_user_cnt` 指标名」的问题。
2. 指标存在性（must be registered）。
3. 维度合法性（时间维度验粒度枚举；其余必须是注册属性）。
4. 过滤合法性（field/operator 枚举/value 必填）。
5. 时间完整性（有时间维度缺 time_range → warning，引擎按 t-1 兜底）。
6. 排序/限量合法性。

返回结构化 `{ok, errors, warnings}`，由 agent 决定自动修正或反问——不直接抛给用户。

### 3.3 `translator.py`（443 行）—— 翻译引擎（核心）

**方法地图**：
```
translate()            入口：捕获 TranslateError/KeyError → {"error"}
├── _metric_items()    兼容 v1.0 metric / v1.1 metrics 列表
├── _translate()       按 owner 分组 → 单表 or 多表；组装 metadata
│   ├── _single_table_sql()  同 owner：选表+SELECT+WHERE+GROUP BY
│   │   ├── _select_table_multi()  ⭐ 表选择（分桶+评分）
│   │   ├── _metric_selects()     预聚合列 or 公式编译
│   │   ├── _dim_expr()           维度列 → 表.列（时间维度→粒度表达式）
│   │   ├── _map_required()       required_filter → F.列
│   │   ├── _filter_sql()         过滤 → 列 + 操作符
│   │   ├── _permission_clause()  权限注入
│   │   └── _time_sql()           时间范围 → dt 分区条件
│   └── _multi_table_sql() 跨 owner：CTE + FULL JOIN / CROSS JOIN
├── _build_joins()      JOIN 链（relations BFS）
├── _compile_formula()  公式白名单编译
├── _order_limit()      排序/限量
```

**metadata 输出**（可审计性）：`metrics / metric_versions / fact_table / tables_used / pre_aggregated / injected_filters / time_range / granularity / dialect / dialect_verified / multi_table`。

### 3.4 `executor.py`（84 行）—— 只读执行

- 双重拦截：`FORBIDDEN` 正则（禁写禁 DDL）+ SQLite `mode=ro` URI 只读打开。
- `iso_week` 方言函数注册（翻译引擎 week 粒度表达式依赖——SQLite 无法解析 `YYYYMMDD` 紧凑格式）。
- 行数上限 1000，`fetchmany(MAX_ROWS+1)` 判断截断。
- **方言与介质解耦**：`dialect=="sqlite"` → SQLite；非 sqlite + `.db` 介质 → 明确报错（不硬跑）；否则 `_execute_remote`（真实数仓扩展点，未实现时 `NotImplementedError`）。

---

## 4. 工具面：`mcp_servers/server.py`（10 个工具）

| 工具 | 层 | 输入→输出 | 说明 |
|------|----|----------|------|
| ontology_search | OAG | 文本→命中清单 | 对象/指标/属性，只返回注册内容 |
| ontology_traverse | OAG | 对象名→关系图 | BFS，含 join_key/required_filter |
| term_normalize | OAG | 文本→{normalized, mappings} | 黑话归一（Step 0） |
| metric_disambiguate | 识别 | 文本→{status, exact/family/candidates} | 指标族三级识别 |
| mql_validate | 安全 | MQL→{ok, errors} | 物理渗入检测 |
| mql_explain | 确认 | MQL→中文确认信息 | 口径/版本/维度含义/过滤/时间 |
| semantic_translate | 执行 | MQL+user+dialect→SQL+metadata | 确定性翻译 |
| execute_sql | 执行 | SQL→结果集 | 只读 |
| ddl_generate | ETL | 对象+层→DDL | 遵守命名规范（Doris 风格标注） |
| scheduler_submit | ETL | task_spec→错误提示 | **预留占位**（待接平台 MCP） |

---

## 5. 智能层：DSH 侧

### 5.1 persona（`agent.cordis.yml`，9 条硬性规则）
先 plan → OAG → MQL 校验 → **mql_explain 用户确认** → 翻译执行 → 口径标注；默认 t-1；多版本/多口径规则；术语归一+回译；ETL 走 D 且先审批；不编造。

### 5.2 8 个 skills（路径闭环）
| Skill | 对应方案 | 职责 |
|-------|---------|------|
| plan-routing | §4.3 | 意图分类(L1-L4) + 路径选择(A/C/B/D) |
| oag-retrieval | §6.2 | OAG 五步 + Step 0 术语归一 + 回译 |
| mql-authoring | §5 | MQL 规范 + 指标族三级识别 + 多指标 |
| path-a-query | §4.2 | 路径 A 全流程（含确认闸门） |
| path-c-skill | §10 | Skill 直执行 |
| path-b-fallback | §3.2 | 探索性长尾兜底 |
| path-d-etl | §4.3 | ETL（DDL/调度/注册/审批） |
| warehouse-standards | 治理 | 分层/主题/命名规范（地址可替换） |

### 5.3 setup_dsh.py / install.sh
幂等：写 cordis.patch.yml（MCP 行）→ 补 dsh-mcp-client 依赖 → 复制 preset + 替换 `{{BACKEND}}`。install.sh 统一 `backend/.venv`（uv run --project backend 单一入口）。

---

## 6. 数据与评测

- **seed**：15 表（DWD×4/DWS×4/ADS×3/DIM×4），统一 `dt` 分区，字段跨层冗余；**DWS/ADS 由 DWD 聚合生成**（三层口径一致）；固定种子可复现；10% 无效单让过滤有意义。
- **builder**：PRAGMA 读表结构 → 推断对象/映射/关系/粒度 → YAML 骨架（人工补口径）。
- **eval**：21 条金标准（12 基础 + 7 多指标 + 2 指标族），断言：校验/表选择/可执行/行数/结果列/多表合并；通过率 ≥90% 门禁；GitHub Actions CI。

---

## 7. 端到端执行过程详解

### 7.1 完整链路（一次取数查询）

```
① 用户提问 ──▶ DSH agent（数据助理预设）
② plan-routing：意图分类 → 复杂度(L1-L4) → 路径选择(A/C/B/D)
③ path-a：oag-retrieval skill
     Step0 term_normalize（黑话→标准术语，记录回译映射）
     Step1 ontology_search（实体/指标定位）
     Step2 版本解析 / metric_disambiguate（口径确认）
     Step3 ontology_traverse（关系图：join_key/required_filter）
④ mql-authoring：生成 MQL → mql_validate 校验
⑤ mql_explain → 中文确认信息 → ask_user_question 用户确认
⑥ semantic_translate（确定性翻译，含权限注入/表选择/JOIN/时间）
⑦ execute_sql（只读执行）→ 结果集
⑧ 口径标注回答（指标名+口径+版本+公式+过滤+数据区间）
```

### 7.2 逐步责任矩阵

| 步骤 | 执行者 | 调用 | 返回 | 约束 |
|------|--------|------|------|------|
| ② 规划 | LLM | skill | 意图/复杂度/路径 | skill 指令 |
| ③ 理解 | LLM+工具 | term_normalize/ontology_* | 归一文本/实体/关系 | 工具供给受限 |
| ④ 生成 MQL | LLM | mql_validate | MQL/校验结果 | 校验器兜底 |
| ⑤ 确认 | LLM+用户 | mql_explain/ask_user | 确认/修改/拒绝 | persona 强制 |
| ⑥ 翻译 | 引擎 | semantic_translate | SQL+metadata | 零 LLM |
| ⑦ 执行 | 引擎 | execute_sql | 结果集 | 只读+限行 |
| ⑧ 回答 | LLM | — | 口径标注文本 | persona 规则 |

### 7.3 实例走查 1：单指标 + 多表 JOIN（真实数据）

用户：**「华东区数码类产品的 GMV，按门店城市拆分，最近 30 天」**

```
② plan：取数 → L2 → 路径 A
③ 黑话归一：无黑话；实体：Product(数码)/Region(华东)/Store(城市)；指标 gmv
   关系扩展：Payment --product_id--> Product / --store_id--> Store / --region_id--> Region
④ MQL：{"metrics":[{"name":"gmv"}], "dimensions":[{"name":"city"}],
         "filters":[{"field":"product_type","operator":"eq","value":"数码"},
                    {"field":"region_name","operator":"eq","value":"华东"}],
         "time_range":{"start":"-30d","end":"today"}}
⑤ 确认：「GMV（支付口径 v1.0：SUM(pay_amount)，过滤 is_valid=1）按门店城市分组，最近30天」
⑥ 翻译引擎：
   表选择 → Payment 域：ads/dws 无 city/product_type/region_name → 排除；
             dwd_ord_pay_di（joinable）→ 命中
   JOIN 链 → dim_product P / dim_store S / dim_region R（join_key 来自 relations）
   权限 → user.region_ids 非空则注入 F.region_id IN(...)
   时间 → F.dt BETWEEN strftime('%Y%m%d',date('now','-30 days')) AND strftime('%Y%m%d','now')
   产出 SQL：
     SELECT (SUM(F.pay_amt)) AS gmv, S.city AS city
     FROM dwd_ord_pay_di F
     JOIN dim_product P ON P.product_id = F.product_id
     JOIN dim_store S ON S.store_id = F.store_id
     JOIN dim_region R ON R.region_id = F.region_id
     WHERE F.is_valid = 1 AND P.product_type='数码' AND R.region_name='华东'
       AND F.dt >= ... AND F.dt <= ...
     GROUP BY S.city ORDER BY gmv DESC
⑦ 执行 → [{上海: 37550.67}, {南京: 31598.79}, {杭州: 22926.65}, {苏州: 39312.36}]
⑧ 回答 → 「GMV（支付口径 v1.0：SUM(pay_amount)，过滤 is_valid=1，数据区间 -30d~today）
           上海 37,550.67 元 / 南京 31,598.79 元 / …」
```

### 7.4 实例走查 2：跨域多指标（CTE 合并）

用户：**「最近 30 天 GMV 和退款金额，按门店类型」**

```
MQL：{"metrics":[{"name":"gmv"},{"name":"refund_amount"}],
      "dimensions":[{"name":"store_type"}], "time_range":{"start":"-30d","end":"today"}}
翻译引擎：
  按 owner 分组 → {Payment:[gmv], Refund:[refund_amount]} → 跨 owner
  每个 owner 生成子查询（各自选表+JOIN dim_store）：
    WITH _m0 AS (SELECT SUM(F.pay_amt) AS gmv, S.store_type AS store_type
                 FROM dwd_ord_pay_di F JOIN dim_store S ... GROUP BY S.store_type),
         _m1 AS (SELECT SUM(F.refund_amt) AS refund_amount, S.store_type AS store_type
                 FROM dwd_ord_refund_di F JOIN dim_store S ... GROUP BY S.store_type)
    SELECT COALESCE(_m0.store_type, _m1.store_type) AS store_type,
           _m0.gmv, _m1.refund_amount
    FROM (_m0 FULL OUTER JOIN _m1 ON _m0.store_type = _m1.store_type)
执行 → [{加盟: gmv=966258.85, refund=24493.56}, {直营: gmv=1238015.37, refund=22621.03}]
```

### 7.5 实例走查 3：黑话 + 指标族（理解层前置处理）

用户：**「华东区 poi 的 goods 销售额，按门店类型拆」**

```
③ Step0 term_normalize → "华东区 门店 的 产品 gmv，按store_type拆"
    mappings: [poi→门店, goods→产品, 销售额→gmv]
③ metric_disambiguate("销售额") → 黑话命中 → exact gmv（支付口径，族默认）
④ MQL → ⑤ 确认（标注支付口径）→ ⑥⑦ 执行
⑧ 回答用词跟随用户：用「门店」而非「门店」的标准名回译
```

### 7.6 ETL 路径 D 走查

用户：**「帮我新建一个按周汇总的用户复购率指标」**
```
plan → 意图=ETL → L4 → 路径 D（path-d-etl skill）
① ontology_search 查重（未注册）
② 拆子任务：设计表结构 / ETL 代码 / 调度 / 注册 Ontology / 审批
③ ddl_generate(obj, DWS) → 表名按规范 = dws_xxx_1d + dt 分区 + is_valid + COMMENT
④ scheduler_submit → 【预留】返回「未接入平台 MCP（mcp-scheduler）」
⑤ 审批（ask_user_question；长链路 task-board 异步）→ 落地
```

### 7.7 异常链路

| 场景 | 行为 |
|------|------|
| MQL 物理渗入（用户说 dwd_xxx/pay_amt） | validator 拒绝 → agent 修正为业务属性或反问 |
| 指标未注册（"客户满意度"） | validator 拒绝 → 候选列表 → 澄清 |
| 表选择失败（粒度/维度不可覆盖） | translator 返回 error → 走降级链（映射缺失→治理建议；探索性→路径 B） |
| 有行级权限但只有 ads 表 | 表选择排除无 perm_column 的表 → 回落 dws/dwd |
| doris 方言 + sqlite 介质 | executor 明确报「方言不匹配，请接 _execute_remote」 |
| 多版本对象无默认 | resolve_version 返回 None → agent 反问用户 |

---

## 8. 关键技术细节

### 8.1 表选择决策算法（`_select_table_multi`）

```
候选过滤：粒度覆盖（_gran_ok：gran∈表粒度 或 day 可聚合成更大粒度）
         × 权限（perm_rids 时须有 perm_column）
         × 指标覆盖（非预聚合指标必须落明细表）
         × 维度覆盖（direct：available_dims 全含；或 joinable：明细表可 JOIN 维度表）
分桶：cands_preagg（预聚合表全指标覆盖+维度直连）vs cands_detail（明细表公式）
选表：preagg 优先（gold > ADS/DWS 层序）；否则 detail（gold 优先）
失败：raise → 降级链
```
体现「月表答不了周查」「有权限时 ads 表不可用」等真实场景。

### 8.2 JOIN 链构建与别名
- `_build_joins`：对每个 needed 维度属性 → `join_path` BFS → 逐 hop `JOIN {dim表} {别名} ON {别名}.{join_key} = {父别名}.{join_key}`，同表只 JOIN 一次。
- 别名：事实表固定 `F`；维度对象别名由 loader **动态生成**（仅 DIM 层、首字母大写、避开 F、冲突加序号）——主题无关且确定性。

### 8.3 公式编译白名单（`_compile_formula`）
1. 属性名 → `F.{物理列}`（查所选表 field_mapping，不依赖全局注册）；
2. 移除 `F.xxx` 后剩余标识符仅允许 `SUM/COUNT/AVG/MAX/MIN/DISTINCT`；
3. 物理表名正则兜底。
三重防护，杜绝公式注入与幻觉列。

### 8.4 多指标合并
- 同 owner：单表多列（全预聚合 或 全明细公式）。
- 跨 owner：CTE（`WITH _m0 AS(...), _m1 AS(...)`）+ 按共同维度 `FULL OUTER JOIN`（COALESCE 对齐）；无维度 `CROSS JOIN` 单行标量。
- **无维度时预聚合列包 `SUM()`** 跨分区聚合（否则返回逐分区多行，语义错误）。

### 8.5 时间系统
- 缺省 → `dt = 昨天`（t-1）；相对表达式（`-30d`/`today`/`last_month_start`...）→ SQL；
- 粒度表达式分 sqlite/doris 两套：sqlite 用 `substr` 字符串运算 + 自定义 `iso_week()`（SQLite 无法解析 `YYYYMMDD` 紧凑格式）；doris 用 `DATE_FORMAT` 等（标注预留）。

### 8.6 权限注入（步骤 0）
`user.region_ids` → `F.{perm_column} IN (...)`，作为 mandatory_clause 注入 WHERE，**不可跳过**；无 perm_column 的表在选表阶段即被排除。

### 8.7 物理渗入检测（精确集合 vs 正则）
物理表名 → 前缀正则（`dwd_/dws_/...`，兜底强信号）；物理列名 → `physical_names` 精确集合（从 Ontology 收集，**减业务名**避免误伤 `order_user_cnt`）。比后缀正则更精确、主题无关。

### 8.8 指标族三级识别（`metric_disambiguate`）
黑话命中（glossary type=metric）→ 口径词命中（"下单GMV"→variant_label 匹配）→ 精确指标名 → 族匹配（返回 variants+差异+default）→ 候选。
skill 侧决策：明示口径用变体；只说族名用默认+标注；问差异则召回确认。

### 8.9 术语归一与回译（`normalize_terms`）
- 最长匹配（词长降序）；只收录"真黑话"（标准名自身不替换）；归一目标 = 展示名（对象用中文 display_name）。
- 回译：mappings 保留 raw→display，回答用词跟随用户。

### 8.10 方言系统与 DIALECT_VERIFIED
```python
DIALECT_VERIFIED = {"sqlite": True, "doris": False}
```
metadata 恒带 `dialect_verified`；doris 分支注释「预留映射，未启用、未验证」；executor 方言与介质解耦（doris+sqlite 明确报错）。**消除"Doris 已支持"的误导**。

### 8.11 版本解析（`resolve_version`）
单版本直取 / `default_version` / None（调用方反问）。与指标族（并行口径）互补：前者是时间演进，后者是并行口径。

---

## 9. 与设计方案（修订版 v2.0）映射核对

| 方案要点 | 实现 | 状态 |
|---------|------|------|
| 理解与执行分离 | MQL 契约 + core 零 LLM | ✅ |
| OAG 五步 | skills + 4 个 OAG 工具（含 Step0 归一） | ✅（文档检索 Step4 待接） |
| MQL v1.1 校验 | validator | ✅ |
| 翻译引擎确定性 | translator | ✅ |
| 多表 JOIN 键不猜 | relations + join_path | ✅ |
| 多指标 | 同域/跨域 | ✅ |
| 行级权限 | perm_column 注入 | ✅ |
| 默认 t-1 | time_sql | ✅ |
| 版本三级规则 | resolve_version | ✅ |
| 多口径指标族 | family + metric_disambiguate | ✅（本轮新增） |
| 业务黑话 | glossary + term_normalize + 回译 | ✅（本轮新增） |
| Plan→路径 | plan-routing + 4 路径 skills | ✅ |
| MQL 用户确认 | mql_explain + persona | ✅ |
| ETL 路径 D | ddl_generate + scheduler_submit | ⚠️ 调度预留 |
| Skill 体系 | 8 skills | ✅ |
| 评测门禁 | 21 条 + CI | ✅ |
| 五职能 Agent | 主 agent + skills | ⚠️ 精简（符合 §11.6） |
| 反馈飞轮 | 未接 | 🔲 |
| 真实数仓执行 | _execute_remote | 🔲 |

---

## 10. 已知边界、预留与风险

**简化点（⚠️）**
1. 多 Agent 未物理拆分（主 agent + skills，符合 §11.6 精简建议）。
2. OAG 文档检索（Step 4）未实现（依赖 FAQ 库，P1 接入）。
3. 跨 owner ≥3 指标 FULL JOIN 以首表维度为锚（2 指标完整对齐）。
4. 行级权限只支持 `region_ids` 一维（多权限维度需扩展 perm_column 多值）。

**预留（🔲）**
1. ETL 调度：`scheduler_submit` 占位，待接平台 mcp-scheduler。
2. ETL 代码模板（Spark/Flink）。
3. 反馈飞轮（dsh-message-feedback → golden 自动入库）。
4. 真实数仓执行：`_execute_remote` + Doris 方言回归验证。

**风险点（⚠️）**
1. JOIN 键正确性完全依赖 `relations.yaml`——错误导致「结果错误但不报错」，真实数仓接入前必须人工核对。
2. `field_mapping` 缺列 → `KeyError`（translate 有兜底返回 error，但需留意）。
3. week 粒度依赖 `iso_week` 方言函数——切真实数仓需确认对应引擎函数。
