-- ════════════════════════════════════════════════════════════
-- 本体结构丰富化迁移（2025-08）
-- 背景：objects/functions/relations 增加治理字段（domain/status/owner/tags…），
--       为 OAG 域路由与状态感知检索提供支持。
-- 适用：已在旧版 schema.sql 建过表的 Supabase 实例。
-- 执行：Supabase SQL Editor 直接运行（幂等：列已存在时跳过报错可忽略）。
-- 新装实例：直接执行 schema.sql 即可（已含下列列），无需本脚本。
-- ════════════════════════════════════════════════════════════

-- ── 表 2：ontology_objects ─────────────────────────────────
ALTER TABLE ontology_objects ADD COLUMN IF NOT EXISTS domain           TEXT NOT NULL DEFAULT '';
ALTER TABLE ontology_objects ADD COLUMN IF NOT EXISTS object_type      TEXT NOT NULL DEFAULT 'fact';
ALTER TABLE ontology_objects ADD COLUMN IF NOT EXISTS status           TEXT NOT NULL DEFAULT 'active';
ALTER TABLE ontology_objects ADD COLUMN IF NOT EXISTS data_owner       TEXT NOT NULL DEFAULT '';
ALTER TABLE ontology_objects ADD COLUMN IF NOT EXISTS tags             JSONB NOT NULL DEFAULT '[]';
ALTER TABLE ontology_objects ADD COLUMN IF NOT EXISTS security_level   TEXT NOT NULL DEFAULT 'L2';
ALTER TABLE ontology_objects ADD COLUMN IF NOT EXISTS update_frequency TEXT NOT NULL DEFAULT 'T+1';

COMMENT ON COLUMN ontology_objects.domain IS '业务域命名空间（ord 订单/usr 用户/prd 产品/fin 财务…），跨域查询按域路由';
COMMENT ON COLUMN ontology_objects.object_type IS '对象类型：fact（事实）/ dim（维度）';
COMMENT ON COLUMN ontology_objects.status IS '生命周期状态：active（在用）/ draft（草稿）/ deprecated（已废弃）';
COMMENT ON COLUMN ontology_objects.data_owner IS '数据责任人（团队/角色），治理归属';
COMMENT ON COLUMN ontology_objects.tags IS '治理标签数组（JSONB）：核心/监管/增长/财务…';
COMMENT ON COLUMN ontology_objects.security_level IS '数据敏感度：L1 公开 / L2 内部 / L3 机密（供行级权限增强）';
COMMENT ON COLUMN ontology_objects.update_frequency IS '数据更新频率：T+1 / 小时 / 实时';

-- ── 表 3：ontology_functions ───────────────────────────────
ALTER TABLE ontology_functions ADD COLUMN IF NOT EXISTS domain     TEXT NOT NULL DEFAULT '';
ALTER TABLE ontology_functions ADD COLUMN IF NOT EXISTS category   TEXT NOT NULL DEFAULT '';
ALTER TABLE ontology_functions ADD COLUMN IF NOT EXISTS status     TEXT NOT NULL DEFAULT 'active';
ALTER TABLE ontology_functions ADD COLUMN IF NOT EXISTS data_owner TEXT NOT NULL DEFAULT '';
ALTER TABLE ontology_functions ADD COLUMN IF NOT EXISTS unit       TEXT NOT NULL DEFAULT '';
ALTER TABLE ontology_functions ADD COLUMN IF NOT EXISTS tags       JSONB NOT NULL DEFAULT '[]';

COMMENT ON COLUMN ontology_functions.domain IS '业务域（缺省跟随 owner 对象），供跨域检索路由';
COMMENT ON COLUMN ontology_functions.category IS '指标分类（规模/质量/效率/增长/财务…）';
COMMENT ON COLUMN ontology_functions.status IS '生命周期状态：active/draft/deprecated';
COMMENT ON COLUMN ontology_functions.data_owner IS '指标责任人（业务方）';
COMMENT ON COLUMN ontology_functions.unit IS '指标单位（元/笔/人/次）';
COMMENT ON COLUMN ontology_functions.tags IS '治理标签数组（JSONB）';

-- ── 表 4：ontology_relations ───────────────────────────────
ALTER TABLE ontology_relations ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT '';

COMMENT ON COLUMN ontology_relations.description IS '关系业务语义说明（OAG traverse 展示，供路径合理性判断）';

-- ════════════════════════════════════════════════════════════
-- 迁移后建议：
--   1) 重新导入 YAML 基线（把新治理字段灌入云端）：
--        python -m ontology_import --yaml backend/ontology
--   2) 验证一致性：
--        python -m ontology_sync_check --yaml backend/ontology
-- ════════════════════════════════════════════════════════════
