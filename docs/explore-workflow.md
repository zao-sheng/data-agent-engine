# 探索性取数工作流（路径 E）

> 版本：v1.0 · 对应 commit `ce44c78` 及之后
> 定位：解决「指标未注册时的探索性取数」——找数据源表 → 写 SQL → 受控执行观测 → 稳定后固化注册

---

## 1. 为什么需要探索路径（问题背景）

日常分析工作大量是**探索性取数**：
- 新业务/新诉求，指标尚未注册；
- 需要"先看看数据长什么样"再决定口径；
- 多表关联、百行千行复杂 SQL，纯自然语言难以描述；
- 探索口径观测一段时间、确认稳定后，才正式创建为指标维度。

**核心矛盾**：引擎的安全模型（query_token 绑定翻译产出 SQL）正确且必要，
但把"未注册指标的探索"一并挡死了。探索路径在**同等只读护栏**下开放，
与正式取数链物理隔离。

---

## 2. 整体架构（三阶段）

```
P0 识别 ──▶ P1 执行 ──▶ P2 固化
```

| 阶段 | 能力 | 工具/模块 |
|------|------|-----------|
| **P0 识别** | 探索意图识别 + 会话内 SQL 输入识别 | `intent.py`（EXPLORE_WORDS + `_is_sql_input`）→ `intent_classify` |
| **P1 执行** | 探索 SQL 校验 + 受控执行 | `explore_validate` / `explore_execute`（`core/explore.py`） |
| **P2 固化** | 探索 SQL → 口径草稿 → 注册 | `explore_promote`（`core/explore_promote.py`）→ modeling_plan → ontology_register |

---

## 3. P0 识别：何时进入探索路径

### 3.1 探索意图（intent=explore, path=E）

`intent_classify` 识别两种来源：

**① 探索信号词 + 指标未命中**：
```
"先看看支付数据长什么样" / "大概看看退款情况" / "探索一下消费数据" / "摸底"
```
命中 `EXPLORE_WORDS`（先看看/大概看看/探索/摸底/试试看/有哪些数据…）且指标未注册
→ `intent=explore, path=E`。

**② 会话内直接粘贴 SQL**（最强信号）：
```
SELECT R.region_name, SUM(F.pay_amt) FROM dwd_ord_pay_di F ... WHERE F.dt='20260817'
```
以 SELECT/WITH 开头 + 含 FROM 子句（支持小写/多行 FROM 行首/CTE/markdown 代码块）
→ `intent=explore, path=E`（不经过 NL 语义解析，直接走探索执行）。

### 3.2 路由决策（plan-routing）

```
取数意图
  ├─ 指标已注册 → A（query-metric 正式取数）
  └─ 指标未注册 → 询问是否新建
       ├─ 是 → D（modeling-etl 建模全流程）
       └─ 否 → 询问是否先探索看看数据
            ├─ 是 → E（explore-fallback）
            └─ 否 → 结束（不查询不编造）
```

> 探索不是绕过正式链的常态路径：指标已注册必须走 A；用户要新建走 D。

---

## 4. P1 执行：探索通道（零前端，会话内交互）

### 4.1 三种交互方式

**方式 A · 直接贴 SQL**（最简单，多表/复杂 SQL 首选）：
```
用户贴 SQL（裸 SQL 或 ```sql 代码块）
  → intent_classify 识别 explore
  → explore_validate(sql) 校验
  → explore_execute(sql, token) 执行 → 结果回会话
```

**方式 B · 自然语言起草**：
```
用户说诉求（"先看看华东区支付金额分布"）
  → 找表（metadata_search/ontology_search/traverse）
  → LLM 起草 SQL（代码块展示）→ 询问"确认执行 / 修改后执行"
  → explore_validate → explore_execute
```

**方式 C · 混合增量**（推荐复杂分析）：
```
NL 诉求 → LLM 起草骨架 → 用户对话修改（"加城市维度/按周聚合"）
  → 确认 → explore_validate → explore_execute
```

### 4.2 探索安全护栏（与正式链同等受控）

| 检查 | 实现 | 违规结果 |
|------|------|---------|
| **只读** | `Executor.FORBIDDEN` 正则 + SQLite `mode=ro` | INSERT/DROP/UPDATE/DELETE/PRAGMA → 拒绝 |
| **表名白名单** | SQL 引用的物理表必须 ∈ 已注册表（`candidate_tables`） | 未注册表 → 拒绝并列出表名 |
| **强制分区** | SELECT 必须带 `dt` 条件 | 无分区 → 拒绝（防全表扫描） |
| **行数上限** | `MAX_ROWS=1000` | 超限截断并标记 |
| **探索令牌** | `explore_token` 绑定 SQL 指纹（120s） | 改 SQL 后旧令牌 → 拒绝 |

### 4.3 与正式取数链的隔离

```
正式取数（已注册指标）:  MQL → validate → explain(confirm) → translate → query_token → execute_sql
探索取数（未注册）:      贴 SQL/NL → explore_validate → explore_token → explore_execute
```

- 探索结果标记 `explore=True` + 提示"非注册口径"；
- 探索 SQL 不经过翻译引擎（无 MQL 语义），但只读护栏完全一致；
- 行级权限：公司鉴权接口另行处理（探索结果未注入 region 过滤，已标注）。

---

## 5. P2 固化：探索 → 正式指标（闭环最后一环）

### 5.1 explore_promote 口径提取

观测稳定后，`explore_promote(sql)` 自动反推口径草稿：

| 探索 SQL 片段 | 提取结果 |
|---|---|
| `SELECT SUM(F.pay_amt)` | 候选指标 `SUM(pay_amount)`——**物理列自动反查业务属性** |
| `SELECT COUNT(DISTINCT F.order_id)` | 候选指标 `COUNT(DISTINCT order_id)` |
| `GROUP BY S.store_type` | 候选维度 `store_type` |
| `WHERE F.is_valid = 1` | 候选过滤 `is_valid = 1`（去表前缀，dt 分区排除） |
| `FROM/JOIN` 表 | 候选源表（未注册表 → 生成 object 草稿） |

### 5.2 关键技术点

1. **公式用业务属性名**（`SUM(pay_amount)` 而非 `SUM(F.pay_amt)`）：
   翻译引擎靠 `field_mapping` 映射回物理列，注册后**立即可翻译执行**。
2. **未映射物理列** → 列入 `unmapped_columns`（人工补 field_mapping，不臆造）。
3. **草稿 `status=draft`**（未激活）——必须经用户确认（modeling_plan 清单确认协议）。

### 5.3 固化流程

```
explore_promote(sql)
  → 返回 register 变更草稿（function/object，可编辑）
  → modeling_plan 确认（清单确认协议：一次性整体确认）
  → ontology_register 固化（status=draft → active）
  → 新指标立即可用（翻译引擎自动映射物理列）
```

---

## 6. 完整闭环示例（真实 SQL）

```sql
-- 用户探索：按门店类型的支付金额分布（指标未注册）
SELECT S.store_type, SUM(F.pay_amt) AS gmv
FROM dwd_ord_pay_di F
JOIN dim_store S ON S.store_id = F.store_id
WHERE F.is_valid = 1 AND F.dt BETWEEN '20260801' AND '20260817'
GROUP BY S.store_type
```

```
① 贴 SQL → intent_classify: intent=explore, path=E
② explore_validate: 通过（表名 dwd_ord_pay_di/dim_store ∈ 白名单，含 dt 条件）
③ explore_execute: 返回 2 行结果（观测）
④ explore_promote: 提取草稿
     - function: store_type_gmv = SUM(pay_amount)（owner=Payment, dims=[store_type], filter=[is_valid=1]）
⑤ modeling_plan 确认 → ontology_register 注册（激活）
⑥ 翻译验证: SELECT (SUM(F.pay_amt)) AS store_type_gmv ... FROM dwd_ord_pay_di F JOIN dim_store S ...
   （业务属性 pay_amount → 物理列 F.pay_amt 自动映射）✓
```

---

## 7. 相关文件

| 文件 | 职责 |
|------|------|
| `backend/core/explore.py` | 探索校验核心：表名白名单/提取/校验 |
| `backend/core/explore_promote.py` | 探索→固化桥接：口径提取/草稿生成 |
| `backend/core/intent.py` | 探索意图识别（EXPLORE_WORDS + `_is_sql_input`） |
| `backend/mcp_servers/server.py` | 3 个探索 MCP 工具（validate/execute/promote） |
| `dsh-side/.../explore-fallback/SKILL.md` | 探索流程 skill（会话内交互协议） |
| `dsh-side/.../plan-routing/SKILL.md` | 路由决策（含探索分支） |
| `backend/tests/test_explore.py` / `test_explore_promote.py` | 探索测试（21 项） |

---

## 8. 设计原则与边界

- **探索 ≠ 常态取数**：指标已注册必须走 A；探索是未注册口径的"先看数据"。
- **同等安全**：探索通道只读护栏与正式链一致，仅放开"未翻译 SQL 执行"这一项。
- **草稿不自动激活**：固化必须经用户确认，探索口径不静默转为正式指标。
- **可追溯**：探索结果标注非注册口径；固化草稿保留来源 SQL。
- **权限**：行级权限由公司鉴权接口另行保障（探索结果未注入，已标注）。
