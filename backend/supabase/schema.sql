-- ============================================================
-- data-agent-engine · Supabase 本体存储建表脚本
-- 在 Supabase 项目 SQL Editor 中执行（或 psql 执行）。
-- 说明：
--   * 表结构与 ontology_store.py 的 SupabaseOntologyStore 对应
--   * 所有业务数据带 revision（乐观锁）+ is_deleted（软删除）+ updated_by
--   * JSONB 列存 YAML 的原始结构（aliases/properties/source_tables 等），
--     读取时反序列化还原成 YAML 形状，Ontology._build 零改动
--   * 每张表/列附 COMMENT 注释（PostgreSQL COMMENT ON TABLE/COLUMN），
--     便于数据字典/管理端展示
--   * RLS 示例在文件末尾（默认注释掉，生产按需启用）
-- ============================================================

-- ════════════════════════════════════════════════════════════
-- 表 1：ontology_meta —— 本体元信息
-- 作用：记录 schema 版本 / 数据版本 / 来源 commit，供运维核对产物新鲜度
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS ontology_meta (
  key         TEXT PRIMARY KEY,              -- schema_version | data_version | source_commit
  value       TEXT NOT NULL,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE ontology_meta IS '本体元信息：schema_version / data_version / source_commit 等产物溯源键值';
COMMENT ON COLUMN ontology_meta.key IS '元信息键名（schema_version | data_version | source_commit）';
COMMENT ON COLUMN ontology_meta.value IS '元信息值（版本号 / commit 短哈希等）';
COMMENT ON COLUMN ontology_meta.updated_at IS '本行最后更新时间';

-- ════════════════════════════════════════════════════════════
-- 表 2：ontology_objects —— 业务对象（对应 objects.yaml）
-- 作用：数仓业务对象（Order/Payment/...），含中文展示名、别名、
--      属性清单、物理表映射（source_tables）、版本历史
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS ontology_objects (
  id              BIGSERIAL PRIMARY KEY,
  name            TEXT NOT NULL UNIQUE,      -- Order / Payment / ...
  display_name    TEXT NOT NULL DEFAULT '',
  description     TEXT NOT NULL DEFAULT '',
  domain          TEXT NOT NULL DEFAULT '',  -- 业务域命名空间（ord/usr/prd/fin…）
  object_type     TEXT NOT NULL DEFAULT 'fact', -- fact | dim
  status          TEXT NOT NULL DEFAULT 'active', -- active | draft | deprecated
  data_owner      TEXT NOT NULL DEFAULT '',  -- 数据责任人
  tags            JSONB NOT NULL DEFAULT '[]',    -- 治理标签
  security_level  TEXT NOT NULL DEFAULT 'L2', -- L1 公开 / L2 内部 / L3 机密
  update_frequency TEXT NOT NULL DEFAULT 'T+1',
  aliases         JSONB NOT NULL DEFAULT '[]',       -- ["订单","下单"]
  required_filters JSONB NOT NULL DEFAULT '[]',      -- ["is_valid = 1"]
  properties      JSONB NOT NULL DEFAULT '[]',       -- [{name,type,unit,aliases,description}]
  source_tables   JSONB NOT NULL DEFAULT '[]',       -- [{table,layer,authority,joinable,
                                                    --   perm_column,granularities,
                                                    --   available_dims,field_mapping,
                                                    --   pre_aggregated,status,description}]
  versions        JSONB NOT NULL DEFAULT '[]',       -- [{version,status,effective_from,...}]
  default_version TEXT,
  revision        BIGINT NOT NULL DEFAULT 1,  -- 乐观锁版本号
  is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,    -- 软删除（评审可见）
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by      TEXT
);
COMMENT ON TABLE ontology_objects IS '业务对象（对应 objects.yaml）：数仓业务对象定义，含属性清单与物理表映射';
COMMENT ON COLUMN ontology_objects.id IS '自增主键';
COMMENT ON COLUMN ontology_objects.name IS '对象唯一标识（Order/Payment/...），与 Ontology 对象名一致';
COMMENT ON COLUMN ontology_objects.display_name IS '中文展示名（如「订单」「支付」）';
COMMENT ON COLUMN ontology_objects.description IS '对象业务描述';
COMMENT ON COLUMN ontology_objects.domain IS '业务域命名空间（ord 订单/usr 用户/prd 产品/fin 财务…），跨域查询按域路由';
COMMENT ON COLUMN ontology_objects.object_type IS '对象类型：fact（事实）/ dim（维度）';
COMMENT ON COLUMN ontology_objects.status IS '生命周期状态：active（在用）/ draft（草稿）/ deprecated（已废弃）';
COMMENT ON COLUMN ontology_objects.data_owner IS '数据责任人（团队/角色），治理归属';
COMMENT ON COLUMN ontology_objects.tags IS '治理标签数组（JSONB）：核心/监管/增长/财务…';
COMMENT ON COLUMN ontology_objects.security_level IS '数据敏感度：L1 公开 / L2 内部 / L3 机密（供行级权限增强）';
COMMENT ON COLUMN ontology_objects.update_frequency IS '数据更新频率：T+1 / 小时 / 实时';
COMMENT ON COLUMN ontology_objects.aliases IS '业务别名数组（JSONB），如 ["订单","下单"]，供黑话归一';
COMMENT ON COLUMN ontology_objects.required_filters IS '必要过滤条件数组（JSONB），如 ["is_valid = 1"]';
COMMENT ON COLUMN ontology_objects.properties IS '业务属性数组（JSONB）：[{name,type,unit,aliases,description}]';
COMMENT ON COLUMN ontology_objects.source_tables IS '物理表映射数组（JSONB）：[{table,layer,authority,joinable,perm_column,granularities,available_dims,field_mapping,pre_aggregated,status,description}]';
COMMENT ON COLUMN ontology_objects.versions IS '对象版本历史（JSONB）：[{version,status,effective_from,...}]';
COMMENT ON COLUMN ontology_objects.default_version IS '默认生效版本（多版本时指定；无则需反问）';
COMMENT ON COLUMN ontology_objects.revision IS '乐观锁版本号：每次更新 +1，写入时校验防多人覆盖';
COMMENT ON COLUMN ontology_objects.is_deleted IS '软删除标记：true 表示已删除（导出 YAML 时过滤，评审可见）';
COMMENT ON COLUMN ontology_objects.created_at IS '创建时间';
COMMENT ON COLUMN ontology_objects.updated_at IS '最后更新时间';
COMMENT ON COLUMN ontology_objects.updated_by IS '最后更新人标识（auth 用户）';

-- ════════════════════════════════════════════════════════════
-- 表 3：ontology_functions —— 指标（对应 functions.yaml）
-- 作用：指标口径定义（公式/归属对象/指标族/必过滤/版本），
--      是取数与口径标注的唯一依据
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS ontology_functions (
  id           BIGSERIAL PRIMARY KEY,
  name         TEXT NOT NULL UNIQUE,         -- gmv / order_gmv / ...
  display_name TEXT NOT NULL DEFAULT '',
  description  TEXT NOT NULL DEFAULT '',
  formula      TEXT NOT NULL,
  owner        TEXT NOT NULL,                -- 归属对象名（ontology_objects.name）
  domain       TEXT NOT NULL DEFAULT '',     -- 业务域（缺省跟随 owner 对象）
  category     TEXT NOT NULL DEFAULT '',     -- 指标分类（规模/质量/效率/增长/财务…）
  status       TEXT NOT NULL DEFAULT 'active', -- active | draft | deprecated
  data_owner   TEXT NOT NULL DEFAULT '',     -- 指标责任人（业务方）
  unit         TEXT NOT NULL DEFAULT '',     -- 指标单位（元/笔/人/次）
  tags         JSONB NOT NULL DEFAULT '[]',  -- 治理标签
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
COMMENT ON TABLE ontology_functions IS '指标（对应 functions.yaml）：指标口径定义（公式/归属/指标族/必过滤/版本）';
COMMENT ON COLUMN ontology_functions.id IS '自增主键';
COMMENT ON COLUMN ontology_functions.name IS '指标唯一标识（gmv/order_gmv/...），与 Ontology 指标名一致';
COMMENT ON COLUMN ontology_functions.display_name IS '指标展示名（如「GMV（支付口径）」）';
COMMENT ON COLUMN ontology_functions.description IS '指标口径描述';
COMMENT ON COLUMN ontology_functions.formula IS '指标公式（白名单函数：SUM/COUNT/AVG/MAX/MIN/DISTINCT + 属性名）';
COMMENT ON COLUMN ontology_functions.owner IS '归属业务对象名（引用 ontology_objects.name）';
COMMENT ON COLUMN ontology_functions.domain IS '业务域（缺省跟随 owner 对象），供跨域检索路由';
COMMENT ON COLUMN ontology_functions.category IS '指标分类（规模/质量/效率/增长/财务…）';
COMMENT ON COLUMN ontology_functions.status IS '生命周期状态：active/draft/deprecated';
COMMENT ON COLUMN ontology_functions.data_owner IS '指标责任人（业务方）';
COMMENT ON COLUMN ontology_functions.unit IS '指标单位（元/笔/人/次）';
COMMENT ON COLUMN ontology_functions.tags IS '治理标签数组（JSONB）';
COMMENT ON COLUMN ontology_functions.family IS '指标族名（如 gmv 族：支付/下单/消费三种口径）';
COMMENT ON COLUMN ontology_functions.variant_label IS '族内变体标签（如「支付口径」）';
COMMENT ON COLUMN ontology_functions.default_of_family IS '是否为族默认口径（取数只说族名时用默认）';
COMMENT ON COLUMN ontology_functions.required_filters IS '必要过滤条件数组（JSONB），如 ["is_valid = 1"]';
COMMENT ON COLUMN ontology_functions.supported_dimensions IS '支持的维度属性数组（JSONB）';
COMMENT ON COLUMN ontology_functions.supported_granularities IS '支持的时间粒度数组（JSONB）：day/week/month/quarter/year';
COMMENT ON COLUMN ontology_functions.do_not IS '口径易错提示（禁止事项），供确认环节展示';
COMMENT ON COLUMN ontology_functions.version IS '指标版本号（v1.0/v2.0...）';
COMMENT ON COLUMN ontology_functions.revision IS '乐观锁版本号';
COMMENT ON COLUMN ontology_functions.is_deleted IS '软删除标记';
COMMENT ON COLUMN ontology_functions.created_at IS '创建时间';
COMMENT ON COLUMN ontology_functions.updated_at IS '最后更新时间';
COMMENT ON COLUMN ontology_functions.updated_by IS '最后更新人标识';

-- ════════════════════════════════════════════════════════════
-- 表 4：ontology_relations —— 关系边（对应 relations.yaml）
-- 作用：对象间 JOIN 关系（JOIN 键唯一来源），翻译引擎永不猜测 JOIN
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS ontology_relations (
  id           BIGSERIAL PRIMARY KEY,
  source       TEXT NOT NULL,                -- 对象名
  target       TEXT NOT NULL,
  type         TEXT NOT NULL DEFAULT '',
  join_key     TEXT NOT NULL,
  cardinality  TEXT NOT NULL DEFAULT 'N:1',
  description  TEXT NOT NULL DEFAULT '',     -- 关系业务语义说明
  revision     BIGINT NOT NULL DEFAULT 1,
  is_deleted   BOOLEAN NOT NULL DEFAULT FALSE,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by   TEXT,
  UNIQUE (source, target, join_key)
);
COMMENT ON TABLE ontology_relations IS '关系边（对应 relations.yaml）：对象间 JOIN 关系，JOIN 键唯一来源';
COMMENT ON COLUMN ontology_relations.id IS '自增主键';
COMMENT ON COLUMN ontology_relations.source IS '源对象名（引用 ontology_objects.name）';
COMMENT ON COLUMN ontology_relations.target IS '目标对象名（引用 ontology_objects.name）';
COMMENT ON COLUMN ontology_relations.type IS '关系类型（contains/fulfills/belongs_to/refunds...）';
COMMENT ON COLUMN ontology_relations.join_key IS 'JOIN 键（物理列名，如 store_id/region_id）';
COMMENT ON COLUMN ontology_relations.cardinality IS '基数（N:1 / 1:N / 1:1）';
COMMENT ON COLUMN ontology_relations.description IS '关系业务语义说明（OAG traverse 展示，供路径合理性判断）';
COMMENT ON COLUMN ontology_relations.revision IS '乐观锁版本号';
COMMENT ON COLUMN ontology_relations.is_deleted IS '软删除标记';
COMMENT ON COLUMN ontology_relations.created_at IS '创建时间';
COMMENT ON COLUMN ontology_relations.updated_at IS '最后更新时间';
COMMENT ON COLUMN ontology_relations.updated_by IS '最后更新人标识';

-- ════════════════════════════════════════════════════════════
-- 表 5：ontology_glossary —— 黑话词典（对应 glossary.yaml）
-- 作用：业务黑话/别名 → 标准名映射（poi→门店、goods→商品），
--      供 term_normalize / intent_classify 使用
-- ════════════════════════════════════════════════════════════
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
COMMENT ON TABLE ontology_glossary IS '黑话词典（对应 glossary.yaml）：业务黑话/别名 → 标准名映射，供术语归一与意图识别';
COMMENT ON COLUMN ontology_glossary.id IS '自增主键';
COMMENT ON COLUMN ontology_glossary.term IS '黑话/别名（如 poi、goods）';
COMMENT ON COLUMN ontology_glossary.canonical IS '归一目标标准名（如 Store、Product）';
COMMENT ON COLUMN ontology_glossary.type IS '目标类型：object（对象）/ property（属性）/ metric（指标）';
COMMENT ON COLUMN ontology_glossary.revision IS '乐观锁版本号';
COMMENT ON COLUMN ontology_glossary.is_deleted IS '软删除标记';
COMMENT ON COLUMN ontology_glossary.created_at IS '创建时间';
COMMENT ON COLUMN ontology_glossary.updated_at IS '最后更新时间';
COMMENT ON COLUMN ontology_glossary.updated_by IS '最后更新人标识';

-- ════════════════════════════════════════════════════════════
-- 表 6：ontology_tables —— 数仓表元数据（供 metadata_search 检索）
-- 作用：每张数仓表的完整元数据（层/字段/血缘/就绪/粒度），
--      由采集器从 schema + 血缘逻辑提取入库；metadata_search 优先查这里
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS ontology_tables (
  id             BIGSERIAL PRIMARY KEY,
  table_name     TEXT NOT NULL UNIQUE,      -- dwd_ord_pay_di / dim_store / ...
  layer          TEXT NOT NULL DEFAULT '',  -- DWD / DWS / ADS / DIM
  domain         TEXT NOT NULL DEFAULT 'ord',
  subject        TEXT NOT NULL DEFAULT '',
  description    TEXT NOT NULL DEFAULT '',  -- 表业务说明
  granularity    TEXT NOT NULL DEFAULT '',  -- 粒度（明细_原子 / 日 / 月 / 维度）
  partition_col  TEXT NOT NULL DEFAULT '',  -- 分区列（事实表=dt，DIM=空）
  owner_object   TEXT,                      -- 归属 Ontology 对象名（如有）
  fields         JSONB NOT NULL DEFAULT '[]',  -- [{name,type,description,is_pk,is_partition}]
  lineage        JSONB NOT NULL DEFAULT '{}',  -- {source: 上游表, logic: 加工逻辑}
  readiness      TEXT NOT NULL DEFAULT '',  -- 就绪时间（如 T+1）
  row_estimate   BIGINT,                    -- 行数估算（可选）
  revision       BIGINT NOT NULL DEFAULT 1,
  is_deleted     BOOLEAN NOT NULL DEFAULT FALSE,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by     TEXT
);
COMMENT ON TABLE ontology_tables IS '数仓表元数据：每张表的层/字段/血缘/就绪/粒度，metadata_search 的数据源';
COMMENT ON COLUMN ontology_tables.id IS '自增主键';
COMMENT ON COLUMN ontology_tables.table_name IS '物理表名（dwd_ord_pay_di / dim_store / ...）';
COMMENT ON COLUMN ontology_tables.layer IS '数仓分层：DWD（明细）/ DWS（汇总）/ ADS（应用）/ DIM（维度）';
COMMENT ON COLUMN ontology_tables.domain IS '主题域（ord/usr/prd/...）';
COMMENT ON COLUMN ontology_tables.subject IS '主题（pay/order/gmv/...）';
COMMENT ON COLUMN ontology_tables.description IS '表业务说明';
COMMENT ON COLUMN ontology_tables.granularity IS '粒度：明细_原子 / 日 / 月 / 维度';
COMMENT ON COLUMN ontology_tables.partition_col IS '分区列（事实表=dt，DIM 为空）';
COMMENT ON COLUMN ontology_tables.owner_object IS '归属 Ontology 业务对象名（如有）';
COMMENT ON COLUMN ontology_tables.fields IS '字段数组（JSONB）：[{name,type,description,is_pk,is_partition}]';
COMMENT ON COLUMN ontology_tables.lineage IS '血缘（JSONB）：{source: 上游表, logic: 加工逻辑}';
COMMENT ON COLUMN ontology_tables.readiness IS '就绪时间（如 T+1）';
COMMENT ON COLUMN ontology_tables.row_estimate IS '行数估算（可选）';
COMMENT ON COLUMN ontology_tables.revision IS '乐观锁版本号';
COMMENT ON COLUMN ontology_tables.is_deleted IS '软删除标记';
COMMENT ON COLUMN ontology_tables.created_at IS '创建时间';
COMMENT ON COLUMN ontology_tables.updated_at IS '最后更新时间';
COMMENT ON COLUMN ontology_tables.updated_by IS '最后更新人标识';

-- ════════════════════════════════════════════════════════════
-- 表 7：ontology_config —— 全局配置（对应 config.yaml）
-- 作用：主题无关配置（时间维度名/分区列名），引擎零主题耦合的关键
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS ontology_config (
  key          TEXT PRIMARY KEY,             -- time_dimension | partition_column
  value        TEXT NOT NULL,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE ontology_config IS '全局配置（对应 config.yaml）：时间维度名/分区列名等主题无关配置';
COMMENT ON COLUMN ontology_config.key IS '配置键名（time_dimension | partition_column）';
COMMENT ON COLUMN ontology_config.value IS '配置值（JSON 序列化）';
COMMENT ON COLUMN ontology_config.updated_at IS '最后更新时间';

-- ── 索引（读路径全量拉取+内存构建；索引主要服务管理端检索）───
CREATE INDEX IF NOT EXISTS idx_functions_owner   ON ontology_functions(owner);
CREATE INDEX IF NOT EXISTS idx_relations_source  ON ontology_relations(source);
CREATE INDEX IF NOT EXISTS idx_relations_target  ON ontology_relations(target);
CREATE INDEX IF NOT EXISTS idx_glossary_term     ON ontology_glossary(term);
CREATE INDEX IF NOT EXISTS idx_objects_name      ON ontology_objects(name);
CREATE INDEX IF NOT EXISTS idx_functions_name    ON ontology_functions(name);
COMMENT ON INDEX idx_functions_owner  IS '按归属对象查指标（管理端指标检索）';
COMMENT ON INDEX idx_relations_source IS '按源对象查关系（关系图遍历加速）';
COMMENT ON INDEX idx_relations_target IS '按目标对象查关系（反向关系检索）';
COMMENT ON INDEX idx_glossary_term    IS '按黑话词查映射（术语归一加速）';
COMMENT ON INDEX idx_objects_name     IS '按对象名查对象（管理端检索）';
COMMENT ON INDEX idx_functions_name   IS '按指标名查指标（管理端检索）';

-- ============================================================
-- RLS：见独立脚本 backend/supabase/rls.sql
-- 生产启用行级安全时执行 rls.sql（7 表 anon 只读 / authenticated 可写），
-- 本文件不内嵌策略，避免双份维护。
-- ============================================================
-- 快速参考（完整策略见 rls.sql）：
--   ALTER TABLE ontology_objects ENABLE ROW LEVEL SECURITY;
--   CREATE POLICY "read_all" ON ontology_objects FOR SELECT USING (true);
--   CREATE POLICY "write_auth" ON ontology_objects
--     FOR ALL USING (auth.role() = 'authenticated')
--     WITH CHECK (auth.role() = 'authenticated');
