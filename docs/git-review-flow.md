# Git 评审闭环方案（Supabase 多人编辑 → Git 评审）

> 适用：`DATA_AGENT_ONTOLOGY_STORE=supabase` 时，本体由多人编辑直写 Supabase，
> 通过 Git 评审建立「已评审发布基线」。单机 yaml 模式无需本流程（文件即评审）。

---

## 1. 核心模型：两层评审

```
┌─ 草稿层（即时生效）──────────────┐   ┌─ 发布层（Git 评审）────────────────┐
│ 管理端/建模流程 → ontology_register │   │ PR: backend/ontology/*.yaml diff    │
│ 写 Supabase（真源，乐观锁）         │ ─▶│ 评审人 review → approve → 合并      │
│ 引擎读真源 = 最新（草稿）           │   │ 合并后 = 已评审发布基线              │
└──────────────────────────────────┘   └─────────────────────────────────────┘
```

- **草稿**：`ontology_register` 直写 DB，引擎立即可用（多人编辑不阻塞）
- **发布**：`ontology_export` 导出当前真源为 YAML → PR 评审 → 合并（留痕/把关/可回滚）

评审审的是「当前 Supabase 真源里的变更」——export 出的 YAML diff 即变更清单。

---

## 2. 评审流程（5 步）

### Step 1 变更落地（真源）
```bash
# 管理端/建模流程，用户确认后
uv run --project backend python -m ontology_import --yaml backend/ontology  # 或
# 引擎内 ontology_register（建模流程）
```

### Step 2 导出评审副本（一键）
```bash
uv run --project backend python -m ontology_export \
  --url $SUPABASE_URL --key $SUPABASE_KEY \
  --out backend/ontology --branch ontology/update-$(date +%Y%m%d) --pr \
  --title "本体更新: 新增XX指标" --body "变更说明..."
```
自动完成：导出 YAML → 建分支 → 提交 → 推送 → `gh pr create`。

### Step 3 提交 PR
评审人看到 `backend/ontology/*.yaml` 的 diff（objects/functions/relations/glossary/config）。

### Step 4 CI 自动检查（PR 触发，ontology-review.yml）
| 检查 | 命令 | 防什么 |
|------|------|--------|
| 一致性 | `ontology_sync_check` | PR 的 YAML ≠ Supabase 真源（漂移） |
| 编译 | `ontology_compile` | YAML 无法编译成 SQLite 产物 |
| 回归 | eval 29 + 单测 114（sqlite 模式） | 变更破坏引擎行为 |

### Step 5 评审 → 合并 → 发布
- approve → 合并 → 成为已评审基线
- 可选：合并后 CI 编译 SQLite 产物发布（生产用 `ONTOLOGY_STORE=sqlite`）

---

## 3. 回滚

出问题从任一已评审基线恢复：
```bash
git checkout <已评审commit> -- backend/ontology/
uv run --project backend python -m ontology_import --yaml backend/ontology  # 回灌真源
```

---

## 4. 角色分工

| 角色 | 动作 |
|------|------|
| 数据管理员/建模者 | `ontology_register`（草稿）+ `ontology_export --pr`（发起评审） |
| 评审人 | review PR：口径是否合理、命名是否合规（warehouse-standards）、diff 是否意外变更 |
| CI | sync_check / compile / 回归自动把关 |
| 引擎 | 读 Supabase 真源（草稿即时生效），reload 刷新 |

---

## 5. 相关命令速查

```bash
# 导出 + 一键 PR
python -m ontology_export --url ... --key ... --out backend/ontology --branch b --pr --title "..."

# 仅导出（不建 PR）
python -m ontology_export --url ... --key ... --out backend/ontology

# 一致性检查（CI / 手动）
python -m ontology_sync_check --yaml backend/ontology --url ... --key ...

# 回灌（评审合并后同步真源）
python -m ontology_import --yaml backend/ontology --url ... --key ...
```

> 依赖：`gh` CLI（GitHub 官方），`--pr` 模式需要它。仅 `--branch` 不需要。
