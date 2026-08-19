# 本体存储模式对比：YAML vs SQLite vs Supabase

> 本体的三种存储模式，选型指引 + 行为等价性说明。
> 换存储只改 `DATA_AGENT_ONTOLOGY_STORE`，引擎代码零改动（store/writer 接口抽象）。

---

## 一句话选型

| 场景 | 选它 |
|------|------|
| 单机演示 / 开发 / 克隆即跑 | **yaml**（默认，零依赖） |
| 本体很大、要冷启动加速 / 发布基线 | **sqlite**（编译产物，只读） |
| 团队多人共同维护本体（口径/表映射） | **supabase**（真源，多人编辑） |

---

## 三种模式对比

| 维度 | yaml | sqlite | supabase |
|------|------|--------|----------|
| **角色** | Git 评审源（手写/评审） | 编译产物（发布基线） | 多人编辑真源（source of truth） |
| **数据位置** | `backend/ontology/*.yaml` | `backend/ontology.db` | 云端 Supabase（5 张表） |
| **可写** | ✅ 手写 / `ontology_register` | ❌ 只读（写 YAML 后重新编译） | ✅ 多人直写（revision 乐观锁） |
| **Git 评审** | ✅ 天然（文件 diff） | ❌ 二进制 | ⚠️ 需 `ontology_export` 导出 YAML 走 PR |
| **多人协作** | ❌ 文件冲突 | ❌ | ✅ 直写 DB + 乐观锁防覆盖 |
| **依赖** | pyyaml（内置） | sqlite3（内置） | requests + 你的 Supabase 实例 |
| **冷启动** | 快（当前 19KB 无感） | 更快（预构建索引） | 需网络（首次拉取） |
| **离线可用** | ✅ | ✅ | ❌（需网络） |
| **适用** | 单机 / 评审基线 | 发布产物 / CI 验证 | 团队共同维护本体 |

---

## 行为等价性（关键保障）

三种模式产出的 **Ontology 内存索引完全等价**——单测覆盖：
- 数据一致性：YAML ↔ SQLite ↔ Supabase 加载的 objects/functions/relations/glossary/config 逐条一致
- 行为一致：翻译（表选择/JOIN）、校验、元数据检索、意图识别、搜索索引全部等价

所以切换模式不会改变引擎行为，只改变「本体数据从哪来」。

---

## 各模式的完整生命周期

### yaml（默认）
```
编辑 backend/ontology/*.yaml →（Git 提交评审）→ 引擎启动读 YAML → 内存索引
```

### sqlite（编译产物）
```bash
# 发布时：YAML → SQLite（CI/install 里跑）
uv run --project backend python -m ontology_compile --yaml backend/ontology --out backend/ontology.db --commit

# 运行时：引擎读产物
DATA_AGENT_ONTOLOGY_STORE=sqlite DATA_AGENT_ONTOLOGY_DB=backend/ontology.db dsh web
```
> 只读：本体变更必须走「改 YAML → 重新编译」。冷启动收益在本体很大（MB 级）时才明显。

### supabase（多人编辑真源）
```bash
# 1. 建表（一次）：Supabase SQL Editor 执行 backend/supabase/schema.sql
#    可选权限：backend/supabase/rls.sql（anon 只读 / authenticated 可写）

# 2. 配置 backend/.env
DATA_AGENT_ONTOLOGY_STORE=supabase
DATA_AGENT_SUPABASE_URL=https://xxxx.supabase.co
DATA_AGENT_SUPABASE_KEY=service_role_key

# 3. 首次导入样例本体（或从 YAML 迁移）
uv run --project backend python -m ontology_import --yaml backend/ontology

# 4. 启动引擎（读真源）
dsh web
```
**多人编辑闭环**：
```
管理端/建模流程（用户确认后）→ ontology_register（写 Supabase，乐观锁）
  → 自动 reload（当前会话立即用最新本体）
其他成员 → ontology_reload（刷新）
评审 → ontology_export（Supabase → YAML 提交 PR）
CI → ontology_sync_check（YAML ↔ Supabase 一致性）
```

---

## 切换模式的影响

| 操作 | 影响 |
|------|------|
| yaml → sqlite | 引擎读产物；本体变更流程变「改 YAML → 编译」（编译可在 CI/install 自动） |
| yaml → supabase | 引擎读云端；本体变更流程变「ontology_register 直写 DB」；评审走 export |
| sqlite → yaml | 直接切回（读 YAML） |
| supabase → yaml | 先 `ontology_export` 拉回 YAML 评审，再切换 |

> ⚠️ 切换前建议 `ontology_sync_check` 确认数据一致性，避免「以为在用新本体实际读旧数据」的脏状态。

---

## 相关文件

- 接口：`backend/core/ontology_store.py`（读）/ `backend/core/ontology_writer.py`（写）
- 工具：`ontology_compile`（YAML→SQLite）/ `ontology_import`（YAML→Supabase）/ `ontology_export`（Supabase→YAML）/ `ontology_sync_check`（一致性检查）
- MCP：`ontology_register`（写，双通道）/ `ontology_reload`（运行时刷新）
- 建表：`backend/supabase/schema.sql`（6 表 + 注释）/ `backend/supabase/rls.sql`（权限）
- 配置：`backend/.env.example`（`DATA_AGENT_ONTOLOGY_STORE` / `DATA_AGENT_ONTOLOGY_DB` / `DATA_AGENT_SUPABASE_*`）
