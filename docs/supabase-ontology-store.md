# Supabase 本体存储接入方案（阶段 2）

> 前置：`core/ontology_store.py` 的读取层抽象已完成（1A/1B），本方案只需
> 新增一个 `SupabaseOntologyStore` 实现同一接口，Ontology 与全部调用方零改动。
> 定位：**Supabase 为运行真源（多人编辑直写 DB），YAML 为导出评审副本**。

---

## 1. 架构定位

```
┌─ 多人编辑（Web/管理端）───┐
│  直写 Supabase（真源）      │
└──────────┬────────────────┘
           │
   ┌───────▼────────┐   export(导出 YAML 提交评审)   ┌─────────────┐
   │  Supabase DB    │ ─────────────────────────────▶ │ Git 评审流   │
   │  (PostgreSQL)   │ ◀───────────────────────────── │ (YAML diff) │
   └───────┬────────┘   import(评审通过后回灌)        └─────────────┘
           │
   ┌───────▼────────┐
   │ SupabaseOntologyStore.load() │  ← 新增实现
   └────────────────┘
           │
   ┌───────▼────────┐
   │  Ontology（索引）│  ← 调用方零改动（translator/validator/metadata/intent/server）
   └────────────────┘
```

- **读路径**：server 启动 `Ontology(store=SupabaseOntologyStore(cfg))` → 全量拉取 → 构建索引（与 YAML/SQLite 一致）
- **写路径**：仅管理端/Agent 建模流程写 DB（新增 `OntologyWriter` 接口，见 §5）
- **评审闭环**：`supabase export → YAML` 提交 PR → 评审 → 合并 →（可选）回灌 DB 基线

---

## 2. 表结构设计（PostgreSQL / Supabase SQL Editor 直接执行）

```sql
-- ============================================================
-- 本体存储（Supabase 真源）
-- 约定：所有业务数据表带 created_at/updated_at；带 revision 乐观锁；
--       RLS 按 auth 角色控制读写；只读账号仅 SELECT。
-- ============================================================

-- 元信息（schema 版本 / 数据来源 / 当前生效版本）
CREATE TABLE IF NOT EXISTS ontology_meta (
  key         TEXT PRIMARY KEY,            -- schema_version | data_version | source_commit
  value       TEXT NOT NULL,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 业务对象（对应 objects.yaml）
CREATE TABLE IF NOT EXISTS ontology_objects (
  id           BIGSERIAL PRIMARY KEY,
  name         TEXT NOT NULL UNIQUE,        -- Order / Payment / ...
  display_name TEXT NOT NULL DEFAULT '',    -- 中文展示名
  description  TEXT NOT NULL DEFAULT '',
  aliases      JSONB NOT NULL DEFAULT '[]', -- ["订单","下单"]
  required_filters JSONB NOT NULL DEFAULT '[]',
  properties   JSONB NOT NULL DEFAULT '[]', -- [{name,type,unit,aliases,description}]
  source_tables JSONB NOT NULL DEFAULT '[]',-- [{table,layer,authority,joinable,perm_column,
                                             --   granularities,available_dims,field_mapping,
                                             --   pre_aggregated}]
  versions     JSONB NOT NULL DEFAULT '[]', -- [{version,status,effective_from,...}]
  default_version TEXT,
  revision     BIGINT NOT NULL DEFAULT 1,   -- 乐观锁版本号
  is_deleted   BOOLEAN NOT NULL DEFAULT FALSE,  -- 软删除（评审可见）
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by   TEXT                          -- auth 用户标识
);

-- 指标（对应 functions.yaml）
CREATE TABLE IF NOT EXISTS ontology_functions (
  id           BIGSERIAL PRIMARY KEY,
  name         TEXT NOT NULL UNIQUE,        -- gmv / order_gmv / ...
  display_name TEXT NOT NULL DEFAULT '',
  description  TEXT NOT NULL DEFAULT '',
  formula      TEXT NOT NULL,
  owner        TEXT NOT NULL,               -- 归属对象名（Ontology objects.name）
  family       TEXT,
  variant_label TEXT,
  default_of_family BOOLEAN NOT NULL DEFAULT FALSE,
  required_filters JSONB NOT NULL DEFAULT '[]',
  supported_dimensions JSONB NOT NULL DEFAULT '[]',
  supported_granularities JSONB NOT NULL DEFAULT '[]',
  do_not       TEXT NOT NULL DEFAULT '',
  version      TEXT NOT NULL DEFAULT 'v1.0',
  revision     BIGINT NOT NULL DEFAULT 1,
  is_deleted   BOOLEAN NOT NULL DEFAULT FALSE,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by   TEXT
);

-- 关系边（对应 relations.yaml）
CREATE TABLE IF NOT EXISTS ontology_relations (
  id           BIGSERIAL PRIMARY KEY,
  source       TEXT NOT NULL,               -- 对象名
  target       TEXT NOT NULL,
  type         TEXT NOT NULL DEFAULT '',
  join_key     TEXT NOT NULL,
  cardinality  TEXT NOT NULL DEFAULT 'N:1',
  revision     BIGINT NOT NULL DEFAULT 1,
  is_deleted   BOOLEAN NOT NULL DEFAULT FALSE,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by   TEXT,
  UNIQUE (source, target, join_key)
);

-- 黑话词典（对应 glossary.yaml）
CREATE TABLE IF NOT EXISTS ontology_glossary (
  id           BIGSERIAL PRIMARY KEY,
  term         TEXT NOT NULL UNIQUE,        -- 黑话/别名
  canonical    TEXT NOT NULL,               -- 标准名
  type         TEXT NOT NULL,               -- object | property | metric
  revision     BIGINT NOT NULL DEFAULT 1,
  is_deleted   BOOLEAN NOT NULL DEFAULT FALSE,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by   TEXT
);

-- 全局配置（对应 config.yaml）
CREATE TABLE IF NOT EXISTS ontology_config (
  key          TEXT PRIMARY KEY,            -- time_dimension | partition_column
  value        TEXT NOT NULL,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 索引（查询加速：读路径全量拉取+构建内存索引，索引主要服务管理端检索）
CREATE INDEX IF NOT EXISTS idx_functions_owner ON ontology_functions(owner);
CREATE INDEX IF NOT EXISTS idx_relations_source ON ontology_relations(source);
CREATE INDEX IF NOT EXISTS idx_glossary_term ON ontology_glossary(term);
```

---

## 3. 读接口：SupabaseOntologyStore

```python
# backend/core/ontology_store.py 追加
class SupabaseOntologyStore:
    """从 Supabase 读取本体（真源）。需先初始化 client（见 §6）。"""
    def __init__(self, url: str, key: str, schema: str = "public"):
        # supabase-py: create_client(url, key)
        # 生产建议用 service_role key（服务端只读）+ RLS 兜底
        ...

    def load(self) -> OntologyData:
        # 全量拉取（本体规模小，一次拉取构建内存索引；表结构即上面 5 张表）
        objs = self._select("ontology_objects", filter="is_deleted=false")
        fns  = self._select("ontology_functions", filter="is_deleted=false")
        rels = self._select("ontology_relations", filter="is_deleted=false")
        gloss= self._select("ontology_glossary", filter="is_deleted=false")
        cfg  = self._select("ontology_config")
        return OntologyData(
            objects=[self._obj_to_yaml_shape(r) for r in objs],   # DB 行 → YAML 结构
            functions=[...],
            relations=[...],
            glossary=[...],
            config={k: json.loads(v) for k, v in cfg},
        )

    def _obj_to_yaml_shape(self, row) -> dict:
        # JSONB 列（aliases/properties/source_tables/versions）反序列化，
        # 拼成与 objects.yaml 一致的 dict（name/display_name/.../aliases/...）
        ...
```

**关键**：`_obj_to_yaml_shape` 把 DB 行还原成 `objects.yaml` 的 dict 形状，
Ontology._build 完全复用，零索引逻辑改动。

---

## 4. 写接口：OntologyWriter（建模流程落地用）

```python
class OntologyWriter(Protocol):
    """本体写入（多人编辑/建模流程）。读接口只管 load，写接口独立。"""
    def upsert_object(self, obj: dict, user: str) -> None: ...
    def upsert_function(self, fn: dict, user: str) -> None: ...
    def upsert_relation(self, rel: dict, user: str) -> None: ...
    def upsert_glossary(self, term: dict, user: str) -> None: ...
    def upsert_config(self, key: str, value) -> None: ...
```

- 每个 upsert 走 **revision 乐观锁**：`UPDATE ... WHERE id=? AND revision=?`，
  影响行数 0 → 冲突 → 返回「已被他人修改」让用户重新基于最新版编辑
- 建模流程（modeling-workflow 阶段 2 本体注册）确认后调用 writer 落库，
  与 YAML 模式（写 ontology/*.yaml）形成两条落地路径

---

## 5. 冲突策略与权限

| 场景 | 策略 |
|------|------|
| 两人同时改同一对象 | **revision 乐观锁**：更新时带旧 revision，冲突返回 409，前端/Agent 提示基于最新版重编 |
| 对象被删除 | **软删除**（is_deleted=true）：评审/回滚可见，导出 YAML 时过滤 |
| 谁改了什么 | `updated_by` + `updated_at` + audit 表（可选加 ontology_audit 变更历史表） |
| 权限 | Supabase **RLS**：管理端（建模）用编辑角色，只读服务用 anon/service_role 仅 SELECT |

```sql
-- RLS 示例：只读角色仅能 SELECT
ALTER TABLE ontology_objects ENABLE ROW LEVEL SECURITY;
CREATE POLICY "read_all" ON ontology_objects
  FOR SELECT USING (true);
CREATE POLICY "write_auth" ON ontology_objects
  FOR INSERT/UPDATE/DELETE USING (auth.role() = 'authenticated');
-- 生产：编辑走 service_role（服务端）或 authenticated 角色
```

---

## 6. 配置与回退

```bash
# backend/.env
DATA_AGENT_ONTOLOGY_STORE=supabase
DATA_AGENT_SUPABASE_URL=https://xxxx.supabase.co
DATA_AGENT_SUPABASE_KEY=service_role_key   # 服务端只读/写
DATA_AGENT_SUPABASE_SCHEMA=public
```

- **fallback**：`DATA_AGENT_ONTOLOGY_STORE=yaml` 是默认（零依赖）；
  Supabase 连接失败 → 启动自检报「Supabase 不可达，请检查配置或回退 yaml」，
  不静默降级（避免「以为在用新本体实际是老 YAML」的脏状态）
- 本地开发/CI 始终 YAML（clone 即跑）；Supabase 只在部署环境启用

---

## 7. 评审闭环（Supabase 真源 + Git 评审）

```
管理端编辑（直写 Supabase）
  → 快照导出：supabase export → ontology/*.yaml（生成 ontology_export 工具）
  → 提交 PR（YAML diff 可评审）
  → 评审通过合并
  → 可选：CI 里 ontology_compile 编译 SQLite 作为发布基线
```

> 导出工具 `ontology_export.py`：`SupabaseOntologyStore.load() → 写回 5 个 YAML`
> （复用 OntologyData 序列化），保证「DB 真源 → Git 评审副本」闭环。

---

## 8. 落地顺序建议

1. 建表（§2 SQL，Supabase SQL Editor 执行）
2. 实现 `SupabaseOntologyStore.load()`（§3）+ `ontology_export.py`（§7）
3. 实现 `OntologyWriter`（§4）+ RLS（§5）
4. 接入 config/env（§6）+ 启动自检
5. 测试：与 Yaml/Sqlite 一致性的单测（复用 test_ontology_store 模式）

> 依赖：`pip install supabase`（supabase-py）。
