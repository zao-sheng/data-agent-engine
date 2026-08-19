-- ============================================================
-- data-agent-engine · Supabase 本体存储建表脚本
-- 在 Supabase 项目 SQL Editor 中执行（或 psql 执行）。
-- 说明：
--   * 表结构与 ontology_store.py 的 SupabaseOntologyStore 对应
--   * 所有业务数据带 revision（乐观锁）+ is_deleted（软删除）+ updated_by
--   * JSONB 列存 YAML 的原始结构（aliases/properties/source_tables 等），
--     读取时反序列化还原成 YAML 形状，Ontology._build 零改动
--   * RLS 示例在文件末尾（默认注释掉，生产按需启用）
-- ============================================================

-- ── 元信息 ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ontology_meta (
  key         TEXT PRIMARY KEY,              -- schema_version | data_version | source_commit
  value       TEXT NOT NULL,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── 业务对象（对应 objects.yaml）───────────────────────────
CREATE TABLE IF NOT EXISTS ontology_objects (
  id              BIGSERIAL PRIMARY KEY,
  name            TEXT NOT NULL UNIQUE,      -- Order / Payment / ...
  display_name    TEXT NOT NULL DEFAULT '',
  description     TEXT NOT NULL DEFAULT '',
  aliases         JSONB NOT NULL DEFAULT '[]',       -- ["订单","下单"]
  required_filters JSONB NOT NULL DEFAULT '[]',      -- ["is_valid = 1"]
  properties      JSONB NOT NULL DEFAULT '[]',       -- [{name,type,unit,aliases,description}]
  source_tables   JSONB NOT NULL DEFAULT '[]',       -- [{table,layer,authority,joinable,
                                                    --   perm_column,granularities,
                                                    --   available_dims,field_mapping,
                                                    --   pre_aggregated}]
  versions        JSONB NOT NULL DEFAULT '[]',       -- [{version,status,effective_from,...}]
  default_version TEXT,
  revision        BIGINT NOT NULL DEFAULT 1,  -- 乐观锁版本号
  is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,    -- 软删除（评审可见）
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by      TEXT
);

-- ── 指标（对应 functions.yaml）────────────────────────────
CREATE TABLE IF NOT EXISTS ontology_functions (
  id           BIGSERIAL PRIMARY KEY,
  name         TEXT NOT NULL UNIQUE,         -- gmv / order_gmv / ...
  display_name TEXT NOT NULL DEFAULT '',
  description  TEXT NOT NULL DEFAULT '',
  formula      TEXT NOT NULL,
  owner        TEXT NOT NULL,                -- 归属对象名（ontology_objects.name）
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

-- ── 关系边（对应 relations.yaml）──────────────────────────
CREATE TABLE IF NOT EXISTS ontology_relations (
  id           BIGSERIAL PRIMARY KEY,
  source       TEXT NOT NULL,                -- 对象名
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

-- ── 黑话词典（对应 glossary.yaml）─────────────────────────
CREATE TABLE IF NOT EXISTS ontology_glossary (
  id           BIGSERIAL PRIMARY KEY,
  term         TEXT NOT NULL UNIQUE,         -- 黑话/别名
  canonical    TEXT NOT NULL,                -- 标准名
  type         TEXT NOT NULL,                -- object | property | metric
  revision     BIGINT NOT NULL DEFAULT 1,
  is_deleted   BOOLEAN NOT NULL DEFAULT FALSE,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by   TEXT
);

-- ── 全局配置（对应 config.yaml）───────────────────────────
CREATE TABLE IF NOT EXISTS ontology_config (
  key          TEXT PRIMARY KEY,             -- time_dimension | partition_column
  value        TEXT NOT NULL,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── 索引（读路径全量拉取+内存构建；索引主要服务管理端检索）───
CREATE INDEX IF NOT EXISTS idx_functions_owner   ON ontology_functions(owner);
CREATE INDEX IF NOT EXISTS idx_relations_source  ON ontology_relations(source);
CREATE INDEX IF NOT EXISTS idx_relations_target  ON ontology_relations(target);
CREATE INDEX IF NOT EXISTS idx_glossary_term     ON ontology_glossary(term);
CREATE INDEX IF NOT EXISTS idx_objects_name      ON ontology_objects(name);
CREATE INDEX IF NOT EXISTS idx_functions_name    ON ontology_functions(name);

-- ============================================================
-- RLS（默认关闭，生产按需启用）
-- 只读服务：anon 或 service_role 仅 SELECT
-- 管理端：authenticated 可写（或服务端用 service_role 写）
-- ============================================================
-- ALTER TABLE ontology_objects   ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE ontology_functions ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE ontology_relations ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE ontology_glossary  ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE ontology_config    ENABLE ROW LEVEL SECURITY;
--
-- CREATE POLICY "read_all" ON ontology_objects   FOR SELECT USING (true);
-- CREATE POLICY "read_all" ON ontology_functions FOR SELECT USING (true);
-- CREATE POLICY "read_all" ON ontology_relations FOR SELECT USING (true);
-- CREATE POLICY "read_all" ON ontology_glossary  FOR SELECT USING (true);
-- CREATE POLICY "read_all" ON ontology_config    FOR SELECT USING (true);
--
-- CREATE POLICY "write_auth" ON ontology_objects
--   FOR ALL USING (auth.role() = 'authenticated')
--   WITH CHECK (auth.role() = 'authenticated');
-- -- ... 其余表同理
