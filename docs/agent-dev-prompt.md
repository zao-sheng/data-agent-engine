# 本体平台开发提示词（Agent 投喂用）

> 用途：投喂给编码 Agent，驱动其自主开发「企业本体平台」。
> 约束：数据库 Supabase · 后端 Python · 前后端分离 · 与本仓库（data-agent-engine）代码/数据完全隔离。

---

# 任务：开发「企业本体平台」（Ontology Platform）

## 0. 你的角色与工作方式

你是资深全栈工程师（10 年+ 数据平台经验）。请**自主完成开发**：

- 严格按本规格实现，不臆测需求；规格未覆盖处选业界最佳实践，并在 README 记录决策
- **分阶段交付**（P0 → P1 → P2），每阶段必须可运行、可验证、可演示
- 每个功能配测试；每阶段结束运行验收脚本并输出报告
- 提交粒度小、message 清晰；**绝不提交密钥**

---

## 1. 项目目标

构建**企业本体平台**：把企业存量数据资产（指标平台 / 表元数据 / BI 报表 / 业务文档）
转换成本体语义模型，经**专家治理审核**后，通过 **MCP/REST + Ossie 标准格式**
服务各类 Agent（Data Agent、分析 Agent）。

核心闭环：

```
存量资产接入 → 自动生成候选 → 专家审核 → 本体入库
  → 对外服务（Agent 消费本体）→ 使用反馈 → 反哺候选
```

**产品定位（务必理解）**：本体平台是**本体数据的生产者、治理者与服务者**，
是本体数据的 source of truth。它**不做取数执行**——翻译、SQL 生成、执行、
对话与答案生成均属消费方 Agent 的职责。

---

## 2. 硬约束（不可违背）

| 项 | 要求 |
|---|---|
| **数据库** | **Supabase**（Postgres + Auth + RLS）；DDL 用编号 migration 管理，`IF NOT EXISTS` 幂等 |
| **后端** | **Python 3.11+ + FastAPI**；只提供 API/MCP，不做服务端渲染 |
| **前端** | **React 19 + TypeScript 5 + Vite**，独立目录/仓库；仅通过 REST 与后端通信 |
| **前后端分离** | 前端不 import Python；后端不产出 HTML/模板；契约通过 OpenAPI/TS 类型对齐 |
| **图谱可视化** | **Cytoscape.js + fcose 布局**（交互参照 microsoft/Ontology-Playground） |
| **语义交换** | **Apache Ossie** JSON/YAML 规范（导入导出，vendor-agnostic） |
| **凭证安全** | Supabase URL/KEY 只从环境变量读，**绝不硬编码**；`.env` 必须 gitignore |
| **数据隔离** | **与任何外部项目完全隔离**（代码/数据/凭证零依赖）；演示数据**本项目自造**（见 §5） |
| **认证授权** | Supabase Auth（JWT）+ RLS 行级策略；后端校验 JWT 与角色 |
| **API 风格** | RESTful，统一响应 `{ok, data, error, meta}`；错误含 code + message |

---

## 3. 参考项目与借鉴清单（明确"学谁什么"）

| 能力域 | 借鉴来源 | 具体借鉴点 |
|---|---|---|
| **图谱交互** | [microsoft/Ontology-Playground](https://github.com/microsoft/Ontology-Playground) | Cytoscape.js + fcose；pan/zoom/点选看属性/实时搜索过滤；**分屏可视化设计器**（图标+颜色+类型化属性、关系+基数、实时预览、undo/redo≥30、实时校验）；命令面板 ⌘K；深链分享；可嵌入 widget |
| **语义标准** | [apache/ossie](https://github.com/apache/ossie) | JSON/YAML 双格式 + schema 校验；converters（dbt/Cube/LookML 互转）；examples 作参考 |
| **元数据治理** | DataHub / OpenMetadata | 多源 adapter 架构、术语表、业务域、血缘、数据产品概念 |
| **指标语义层** | Cube / MetricFlow / DataJunction | measures / dimensions / entities 三段式；指标族（多口径）；复合指标 |
| **对象模型** | Palantir Ontology | 对象类型、属性、关系（带语义/基数）、函数分层 |
| **自动提取 + 权威** | Databricks Genie | 多源扫描反推语义；**类 PageRank 四维权威评分**（创建者/引用频次/认证关联/时效） |
| **多格式互通** | sidemantic | 已有指标定义（Cube/dbt/LookML）导入而非重写 |

---

## 4. 仓库结构

```
ontology-platform/
├── backend/                          # Python 后端（独立部署）
│   ├── app/
│   │   ├── main.py                   # FastAPI 入口
│   │   ├── api/                      # 路由（objects/functions/relations/glossary/
│   │   │                             #        graph/governance/ingest/ontology-service）
│   │   ├── services/                 # 业务逻辑（映射/审核/权威评分/冲突裁决/图谱构建）
│   │   ├── adapters/                 # 资产接入适配器 + mock 数据源
│   │   ├── ontology/                 # 本体领域逻辑（模型/字段级校验/索引/快照）
│   │   ├── mcp/                      # MCP Server（本体数据服务，只读）
│   │   ├── models/                   # Pydantic 模型（API 契约）
│   │   └── db/                       # Supabase 客户端封装
│   ├── migrations/                   # 编号 SQL migration
│   ├── seed/                         # 演示数据（本项目自造，见 §5）
│   ├── tests/                        # pytest
│   ├── requirements.txt
│   └── .env.example
├── frontend/                         # React 前端（独立部署）
│   ├── src/
│   │   ├── pages/                    # 图谱/设计器/治理台/目录/设置
│   │   ├── components/               # Graph/Inspector/Designer/CandidateQueue/ConflictPanel
│   │   ├── api/                      # 后端 API 客户端（JWT 注入 + 类型安全）
│   │   ├── store/                    # Zustand
│   │   └── types/                    # TS 类型
│   ├── package.json / vite.config.ts
│   └── .env.example
├── docs/                             # architecture / api / deployment
└── README.md
```

---

## 5. 演示与测试数据（本项目自造，严禁引用外部项目）

**本平台是独立产品，必须自造演示数据**——不得拷贝、导入或引用任何外部项目
（如 data-agent-engine）的本体定义、样例库或表结构。

### 5.1 数据形态

```
backend/seed/
├── mock_sources/                    # 模拟「外部资产系统」接口返回（adapter 输入）
│   ├── metric_platform.json         #   指标平台：指标名/公式/维度/负责人/状态
│   ├── metadata_platform.json       #   元数据平台：表/字段/类型/血缘/注释/分区
│   ├── bi_reports.json              #   BI 报表：指标×维度组合 + 引用次数 + 使用人
│   └── docs/*.md                    #   业务文档：术语解释、口径背景
├── seed_ontology.sql                # 演示本体（域/对象/指标/关系/黑话/表元数据）
├── seed_governance.sql              # 演示治理（候选/冲突/权威信号/审计）
└── reset.sql                        # 清空并重建演示数据（幂等，可反复执行）
```

### 5.2 演示数据规格（主题自定，勿与外部项目雷同）

建议自造**零售/会员**主题（4 个域，开发者可替换），规模需覆盖平台全部功能：

| 类别 | 规模 | 必须覆盖的要点 |
|---|---|---|
| 业务域 | 3-4 个 | 如 trade / member / goods / supply |
| 业务对象 | 10-15 个 | fact（订单/支付/退款/核销）+ dim（会员/商品/门店/区域），各带治理字段 |
| 属性 | 每对象 4-8 个 | 含**枚举字段（带 values 值域）**、时间字段（对齐分区列）、必过滤字段 |
| 指标 | 20-30 个 | **必含**：多口径族（如成交额 3 口径）、复合指标（客单价=成交额/单量）、多版本口径、带 `do_not` |
| 关系 | 15-25 条 | 事实→维度 + **事实→事实**（支付→退款）；带 join_key / 基数 / 语义描述 |
| 黑话 | 25-35 条 | 对象级 + 属性级 + 指标级别名 |
| 表元数据 | 15-20 张 | layer / 粒度 / field_mapping / pre_aggregated / **值域分布** / 血缘 |
| 治理候选 | 15-20 条 | 模拟接入生成（多 source_system、不同 confidence、多种 target_type） |
| 冲突记录 | 3-5 条 | 同概念多来源定义冲突（如"活跃会员"多口径） |
| 权威信号 | 50+ 条 | 报表引用/查询命中/责任人/认证/时效（供评分验证） |

### 5.3 要求

1. `reset.sql` 一键重建（幂等），本地开发与测试通用
2. 验收测试用演示数据跑通「接入 → 候选 → 审核 → 图谱 → 服务接口」闭环
3. 前端图谱页应能渲染 10+ 节点、15+ 边的可交互图（多域着色）
4. 数据与逻辑解耦：换主题只改 seed 文件，代码不动
5. README 明确标注「演示数据为项目自造，与任何外部项目无关」

---

## 6. 数据模型（Supabase Postgres）

在 `backend/migrations/` 按序创建，全部幂等。**每张业务表启用 RLS**。

### 6.1 核心本体表

```sql
CREATE TABLE IF NOT EXISTS domain (
  code TEXT PRIMARY KEY, name TEXT NOT NULL,
  parent_code TEXT REFERENCES domain(code),
  owner TEXT, status TEXT DEFAULT 'active',
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ontology_object (
  id BIGSERIAL PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,                -- 业务实体名（非表名）
  display_name TEXT NOT NULL,
  domain_code TEXT REFERENCES domain(code),
  object_type TEXT NOT NULL DEFAULT 'fact', -- fact | dim
  status TEXT NOT NULL DEFAULT 'active',    -- active|draft|deprecated
  data_owner TEXT, security_level TEXT DEFAULT 'L2',
  update_frequency TEXT DEFAULT 'T+1',
  aliases JSONB DEFAULT '[]',
  description TEXT DEFAULT '',
  required_filters JSONB DEFAULT '[]',
  properties JSONB DEFAULT '[]',            -- [{name,type,unit,values,required_filter,description}]
  source_tables JSONB DEFAULT '[]',         -- [{table,layer,authority,joinable,perm_column,
                                            --   granularities,available_dims,field_mapping,pre_aggregated}]
  versions JSONB DEFAULT '[]', default_version TEXT,
  authority_score NUMERIC DEFAULT 0,
  revision BIGINT DEFAULT 1, is_deleted BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now(), updated_by TEXT
);

CREATE TABLE IF NOT EXISTS ontology_function (
  id BIGSERIAL PRIMARY KEY,
  name TEXT UNIQUE NOT NULL, display_name TEXT NOT NULL,
  formula TEXT NOT NULL, owner TEXT NOT NULL,
  domain_code TEXT, category TEXT,
  status TEXT DEFAULT 'active', data_owner TEXT, unit TEXT,
  family TEXT, variant_label TEXT, default_of_family BOOLEAN DEFAULT FALSE,
  required_filters JSONB DEFAULT '[]',
  supported_dimensions JSONB DEFAULT '[]',
  supported_granularities JSONB DEFAULT '[]',
  do_not TEXT DEFAULT '', version TEXT DEFAULT 'v1.0',
  authority_score NUMERIC DEFAULT 0, tags JSONB DEFAULT '[]',
  revision BIGINT DEFAULT 1, is_deleted BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now(), updated_by TEXT
);

CREATE TABLE IF NOT EXISTS ontology_relation (
  id BIGSERIAL PRIMARY KEY,
  source TEXT NOT NULL, target TEXT NOT NULL,
  join_key TEXT NOT NULL, cardinality TEXT DEFAULT 'N:1',
  type TEXT, description TEXT, status TEXT DEFAULT 'active',
  revision BIGINT DEFAULT 1, is_deleted BOOLEAN DEFAULT FALSE,
  UNIQUE(source, target, join_key)
);

CREATE TABLE IF NOT EXISTS ontology_glossary (
  term TEXT PRIMARY KEY, canonical TEXT NOT NULL,
  type TEXT NOT NULL,                       -- object|property|metric
  domain_code TEXT, source TEXT DEFAULT 'manual'
);

CREATE TABLE IF NOT EXISTS table_metadata (
  table_name TEXT PRIMARY KEY,
  layer TEXT, domain_code TEXT, subject TEXT,
  granularity TEXT, partition_col TEXT, owner_object TEXT,
  fields JSONB DEFAULT '[]', value_profiles JSONB DEFAULT '{}',
  lineage JSONB DEFAULT '{}', readiness TEXT,
  row_estimate BIGINT, authority TEXT, revision BIGINT DEFAULT 1
);
```

### 6.2 治理表

```sql
CREATE TABLE IF NOT EXISTS governance_candidate (
  id BIGSERIAL PRIMARY KEY,
  target_type TEXT NOT NULL,        -- object|function|relation|glossary|value_profile
  payload JSONB NOT NULL,
  source_system TEXT, source_ref TEXT,
  confidence NUMERIC DEFAULT 0.5,
  status TEXT DEFAULT 'pending',    -- pending|approved|rejected|merged
  reviewer TEXT, reviewed_at TIMESTAMPTZ, reject_reason TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS governance_conflict (
  id BIGSERIAL PRIMARY KEY, concept TEXT NOT NULL,
  variants JSONB NOT NULL,          -- [{source, definition, authority_score}]
  resolved_variant TEXT, resolution TEXT,
  resolver TEXT, resolved_at TIMESTAMPTZ, status TEXT DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS authority_signal (
  id BIGSERIAL PRIMARY KEY, target_type TEXT, target_name TEXT,
  signal_type TEXT,                 -- report_ref|query_hit|downstream_ref|owner_role|certified|recency
  weight NUMERIC DEFAULT 1, source_ref TEXT,
  observed_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ontology_audit (
  id BIGSERIAL PRIMARY KEY, target_type TEXT, target_name TEXT, action TEXT,
  before JSONB, after JSONB, impact JSONB,
  operator TEXT, operated_at TIMESTAMPTZ DEFAULT now()
);
```

### 6.3 RLS（每张表）

```sql
ALTER TABLE ontology_object ENABLE ROW LEVEL SECURITY;
CREATE POLICY "read_all" ON ontology_object FOR SELECT USING (true);
CREATE POLICY "write_auth" ON ontology_object FOR ALL
  USING (auth.role() = 'authenticated') WITH CHECK (auth.role() = 'authenticated');
-- 其余表同样处理
```

---

## 7. 后端规格（Python + FastAPI）

### 7.1 应用边界

✅ **做**：资产接入与映射（产候选）、专家治理、本体 CRUD 与版本、图谱数据服务、
权威评分、覆盖率、影响分析、对外本体服务接口

❌ **不做**：MQL 解析、SQL 翻译、取数执行、对话与答案生成（Agent 职责）

**与 Data Agent 的关系**：本体平台 = 本体数据源；Agent = 消费方。
通过 MCP（运行时按需拉取）/ REST（管理端）/ Ossie JSON-YAML（批量导出）解耦，
**双方零代码依赖**，平台不感知 Agent 如何翻译执行。

**Supabase 访问**：`supabase-py` 或 PostgREST（urllib，零额外依赖）；凭证从 env 读。

### 7.2 API 端点清单

```
# 本体管理（写，需认证）
GET/POST/PATCH/DELETE  /api/objects            对象 CRUD（校验走 entry_validator 规则）
GET/POST/PATCH/DELETE  /api/functions          指标 CRUD
GET/POST/DELETE        /api/relations          关系 CRUD
GET/POST/DELETE        /api/glossary           黑话 CRUD
GET/POST               /api/domains            业务域
GET                    /api/table-metadata      表元数据（含值域分布）

# 图谱（前端可视化数据）
GET  /api/graph/overview                        全量图（nodes+edges）
GET  /api/graph/object/{name}?depth=2           以对象为中心的子图
GET  /api/graph/domain/{code}                   按域子图
GET  /api/graph/diff?from=&to=                  版本差异图

# 治理
GET  /api/governance/candidates                 候选队列（status/source/type 过滤）
POST /api/governance/candidates/{id}/approve    审核通过 → 写本体（复校验）
POST /api/governance/candidates/{id}/reject     驳回（带原因）
GET  /api/governance/conflicts                  冲突列表
POST /api/governance/conflicts/{id}/resolve     裁决
GET  /api/governance/coverage                   覆盖率看板
GET  /api/audit                                 变更审计（含影响面）

# 资产接入
POST /api/ingest/metrics                        从指标平台拉取 → 产候选
POST /api/ingest/metadata                       从元数据平台拉取 → 产候选
POST /api/ingest/reports                        BI 报表引用 → 维度白名单 + 权威信号
POST /api/ingest/docs                           文档 → 黑话/口径解释候选
POST /api/ingest/sample-values                  采样枚举字段 → 值域候选

# 本体数据服务（供 Agent 消费，只读）
GET  /api/ontology/snapshot                     全量本体快照（供 Agent 加载）
GET  /api/ontology/search?q=&domain=            语义检索（域路由 + 倒排）
GET  /api/ontology/traverse?root=&depth=        关系图遍历
GET  /api/ontology/resolve-metric?q=            指标解析（族/口径/量纲/别名）
GET  /api/ontology/glossary-normalize?text=     术语归一
GET  /api/ontology/changes?since=               增量变更（缓存失效感知）

# 标准格式互通
GET  /api/export/ossie                          导出 Ossie JSON/YAML
POST /api/import/ossie                          导入 Ossie（产候选，不直接入库）

# 运维
GET  /api/health                                健康检查（对象/指标/关系计数）
```

### 7.3 MCP Server（只读本体服务）

独立进程（stdio），**只暴露本体读能力，不含执行**：

```
ontology_snapshot      全量本体（Agent 启动加载/定时刷新）
ontology_search        语义检索（域路由 + 倒排）
ontology_traverse      关系图遍历（join_key/基数/语义）
metric_resolve         指标解析（族/口径/量纲/别名）
glossary_normalize     术语归一
graph_query            图谱查询
ontology_changes       增量变更
```

与 REST 共用 services 层。

### 7.4 关键服务逻辑

- **候选生成**：adapter 拉取 → 映射为 payload → 计算 confidence（来源可信度 + 字段完整度）→ 入候选表
- **审核通过**：校验 payload（类型/枚举/量纲/必填）→ upsert 本体表 → 写 audit
- **权威评分**（定时任务）：聚合 authority_signal 四维加权 → 更新 `authority_score`
  默认权重：报表引用 3 / 查询命中 1 / 下游引用 2 / 责任人 2 / 认证 3 / 时效 1（带时间衰减）
- **冲突检测**：同 concept 多来源定义不一致 → 建冲突记录（附各来源权威分）
- **图谱构建**：对象为节点（domain 着色 / type 区分）、关系为边（标注 join_key + 基数）

---

## 8. 前端规格（React + Cytoscape.js）

### 8.1 技术栈

React 19 + TS 5 + Vite + **Cytoscape.js（fcose）** + Zustand + TanStack Query +
Ant Design（表单密集）+ React Router（深链）。

### 8.2 页面与交互（P0）

**① 图谱浏览页（核心）**
- Cytoscape 全屏图：节点按 domain 着色、按 type 区分形状；边标注 join_key/基数
- 交互：pan/zoom、点节点 → 右侧 Inspector（属性/物理映射/关系/关联指标）
- 实时搜索：输入即过滤并高亮命中节点与边
- 布局切换（fcose/层级/圆形）、按域筛选、fact/dim 筛选
- **深链**：`/#/graph/object/{name}`、`/#/graph/domain/{code}` 可分享
- **命令面板 ⌘K**：跳转图谱/设计器/治理台/对象

**② 可视设计器**
- 分屏：左侧表单（对象/属性/关系/指标）+ 右侧实时图谱预览
- 拖拽连线创建关系（弹窗填 join_key / 基数 / type / 描述）
- undo/redo（≥30 级）、实时校验（缺主键/缺值域/缺必过滤 → 内联提示）
- 保存调后端 API（服务端复校验）

**③ 治理台**
- 候选队列：表格 + **差异对比**（候选 vs 现有）+ 一键通过/驳回（带原因）
- 冲突裁决：并列展示多来源定义 + 权威评分 + 选择采纳 + 填裁决理由
- 覆盖率看板：域覆盖率、未覆盖高频查询 Top N

**④ 目录页**：本体目录（按域/类型浏览）、模板起步（不面对空白页）

### 8.3 API 客户端

统一封装：JWT 注入、错误处理（code→提示）、类型安全（对齐后端 Pydantic/OpenAPI）。

---

## 9. 分期交付

### P0（端到端跑通，必须完整）
1. Supabase migration（§6 全部表 + RLS）
2. 后端：对象/指标/关系/黑话 CRUD + 图谱 3 接口 + 候选队列 3 接口 + health
3. 后端：1 个真实 adapter（对 mock_sources）+ mock 数据源（本项目自造数据）
4. 后端：MCP Server（snapshot / search / graph_query / health）
5. 前端：图谱浏览页 + 对象/指标列表页 + 候选审核页
6. 测试：pytest（CRUD/审核/图谱/服务接口）+ vitest（图谱渲染/搜索过滤）
7. 文档：README（Supabase 建表 → 起后端 → 起前端 → 审核第一条候选，10 分钟内可跑）

### P1
- 表元数据 adapter + 对象骨架生成（血缘 → 对象归并）
- 可视设计器（分屏 + 拖拽连线 + undo/redo + 实时校验）
- Ossie 导入导出 + 至少 1 个 converter（Cube 或 dbt）
- 权威评分定时任务 + 冲突检测

### P2
- 文档抽取（LLM → 黑话/口径候选）+ 值域采样
- 变更审批流 + 影响分析
- 覆盖率看板完善 + 版本差异图谱
- 目录页 + 模板库 + 命令面板完善

---

## 10. 验收标准（每阶段自证）

```bash
# 后端
cd backend && pytest -q                       # 全绿
uvicorn app.main:app --reload                 # 启动无错
curl localhost:8000/api/health                # 返回对象/指标/关系计数

# 前端
cd frontend && npm run build && npm test      # 构建与测试通过
# 浏览器：图谱渲染出节点边、点节点显示 Inspector、搜索可过滤、深链可分享

# 端到端（P0 核心场景）
1. POST /api/ingest/metrics        → governance_candidate 出现候选
2. POST /api/governance/candidates/{id}/approve
                                   → ontology_function 出现记录 + audit 留痕
3. 前端图谱页刷新                   → 新对象/指标可见、可点选查看
4. MCP tools/list                  → 返回已注册工具
   MCP ontology_search("成交额")    → 命中已审核指标
```

---

## 11. 禁止事项（务必遵守）

1. **禁止硬编码 Supabase URL/KEY**——只从环境变量读，`.env` 必须 gitignore
2. **禁止自动写入本体**——所有自动映射只产候选，必须经审核才入本体表
3. **禁止把取数执行逻辑做进平台**——翻译/SQL 生成/执行/对话均属 Agent 职责，
   本体平台只做本体数据的生产、治理与服务
4. **禁止前后端耦合**——前端只通过 REST/MCP 访问后端，不 import Python；后端不产 HTML
5. **禁止跳过 RLS**——每张业务表都要有策略
6. **禁止引用任何外部项目的数据**——不得拷贝/导入其本体定义、样例库或表结构；
   演示数据必须自造（§5）
7. **禁止复用外部项目的主题**——用本项目自造主题，避免混淆与耦合
8. **禁止提交密钥/日志/生成的数据库文件**
9. **禁止一次性全量开发**——严格 P0 → P1 → P2，每阶段可运行

---

## 12. 交付物清单

- [ ] `backend/`（FastAPI + Supabase + adapters + MCP server + pytest）
- [ ] `frontend/`（React + Cytoscape 图谱 + 设计器 + 治理台 + 测试）
- [ ] `backend/migrations/`（幂等 Supabase DDL + RLS）
- [ ] `backend/seed/`（自造演示数据 + mock_sources + reset.sql）
- [ ] `docs/architecture.md`、`docs/api.md`、`docs/deployment.md`
- [ ] `README.md`（10 分钟跑通：建表 → 后端 → 前端 → 审核第一条候选）
- [ ] 每阶段验收报告（命令 + 输出）

---

## 13. 开始之前

先输出：
1. **P0 实现计划**（拆解为任务清单，标注依赖顺序）
2. **关键设计决策确认**（Supabase 访问方式、本体服务接口边界、图谱数据格式、Ossie 字段映射）
3. **需要澄清的问题**（如有）

然后立即开始 P0 开发——不要等所有问题澄清（用合理默认 + README 记录决策）。
