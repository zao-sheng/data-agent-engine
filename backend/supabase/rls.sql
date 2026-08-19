-- ============================================================
-- data-agent-engine · Supabase 本体存储 RLS 启用脚本
-- 在 Supabase SQL Editor 执行（或 psql 执行），为 6 张表启用行级安全。
-- 策略：
--   * anon（公开/只读服务）→ 仅 SELECT（数据字典展示、只读查询）
--   * authenticated（登录用户）→ 读写（多人编辑）
--   * service_role（服务端）→ 绕过 RLS（Supabase 内置），不受本策略影响
--
-- 前置：已执行 backend/supabase/schema.sql 建表。
-- 注意：启用 RLS 后，未认证的写请求会被拒绝——需配合
--       DATA_AGENT_SUPABASE_KEY 使用 service_role 或登录态。
-- ============================================================

-- ── 1. 启用 RLS ────────────────────────────────────────────
ALTER TABLE ontology_meta      ENABLE ROW LEVEL SECURITY;
ALTER TABLE ontology_objects   ENABLE ROW LEVEL SECURITY;
ALTER TABLE ontology_functions ENABLE ROW LEVEL SECURITY;
ALTER TABLE ontology_relations ENABLE ROW LEVEL SECURITY;
ALTER TABLE ontology_glossary  ENABLE ROW LEVEL SECURITY;
ALTER TABLE ontology_config    ENABLE ROW LEVEL SECURITY;
ALTER TABLE ontology_tables    ENABLE ROW LEVEL SECURITY;

-- ── 2. 只读策略（anon / 未登录均可读）──────────────────────
CREATE POLICY "read_all" ON ontology_meta      FOR SELECT USING (true);
CREATE POLICY "read_all" ON ontology_objects   FOR SELECT USING (true);
CREATE POLICY "read_all" ON ontology_functions FOR SELECT USING (true);
CREATE POLICY "read_all" ON ontology_relations FOR SELECT USING (true);
CREATE POLICY "read_all" ON ontology_glossary  FOR SELECT USING (true);
CREATE POLICY "read_all" ON ontology_config    FOR SELECT USING (true);
CREATE POLICY "read_all" ON ontology_tables    FOR SELECT USING (true);

-- ── 3. 写入策略（authenticated 登录用户可写）────────────────
CREATE POLICY "write_auth" ON ontology_meta
  FOR ALL USING (auth.role() = 'authenticated')
  WITH CHECK (auth.role() = 'authenticated');

CREATE POLICY "write_auth" ON ontology_objects
  FOR ALL USING (auth.role() = 'authenticated')
  WITH CHECK (auth.role() = 'authenticated');

CREATE POLICY "write_auth" ON ontology_functions
  FOR ALL USING (auth.role() = 'authenticated')
  WITH CHECK (auth.role() = 'authenticated');

CREATE POLICY "write_auth" ON ontology_relations
  FOR ALL USING (auth.role() = 'authenticated')
  WITH CHECK (auth.role() = 'authenticated');

CREATE POLICY "write_auth" ON ontology_glossary
  FOR ALL USING (auth.role() = 'authenticated')
  WITH CHECK (auth.role() = 'authenticated');

CREATE POLICY "write_auth" ON ontology_config
  FOR ALL USING (auth.role() = 'authenticated')
  WITH CHECK (auth.role() = 'authenticated');

CREATE POLICY "write_auth" ON ontology_tables
  FOR ALL USING (auth.role() = 'authenticated')
  WITH CHECK (auth.role() = 'authenticated');

-- ============================================================
-- 说明
-- ============================================================
-- * 我们的引擎默认用 service_role/secret key 访问（绕过 RLS），
--   所以 RLS 主要保护「从管理端 Web 页面/浏览器直连」的场景：
--   - 匿名访客只能读（数据字典只读展示）
--   - 登录用户（authenticated）才能改
-- * 若你的管理端用 publishable key + 匿名访问，则写入策略需改为：
--     CREATE POLICY "write_anon" ON ontology_objects
--       FOR ALL USING (true) WITH CHECK (true);
--   （不建议生产环境开放匿名写）
-- * 回滚（关闭 RLS）：
--     ALTER TABLE ontology_objects DISABLE ROW LEVEL SECURITY;
--     -- 其余表同理
