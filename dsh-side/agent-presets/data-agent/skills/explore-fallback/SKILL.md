---
name: explore-fallback
description: 探索性取数（路径 E）——指标未注册时，找数据源表→写 SQL→受控执行观测，稳定后可固化注册。支持 NL / 纯 SQL / 混合三模式。
---

# 探索性取数（路径 E）

## 零前端 · 会话内交互（推荐，无需 Monaco 集成）

不需要独立前端——**直接在会话里完成探索**，三种方式任选：

### 方式 A · 直接贴 SQL（最简单）
用户在会话输入框直接粘贴/编写 SQL（支持裸 SQL 或 ```` ```sql ```` 代码块）：
```
用户: SELECT F.region_id, SUM(F.pay_amt) AS amt
      FROM dwd_ord_pay_di F
      JOIN dim_region R ON R.region_id = F.region_id
      WHERE F.dt BETWEEN '20260801' AND '20260817'
      GROUP BY F.region_id

Agent: intent_classify 识别为 explore（path=E，SQL 输入信号）
  → explore_validate(sql) 校验（表名白名单/只读/强制分区）
  → explore_execute(sql, token) 执行 → 返回结果表
```
- 引擎自动识别 SQL 输入（以 SELECT/WITH 开头 + FROM），不走 NL 解析；
- 校验失败会给出具体 errors（未注册表/缺分区/写操作），用户改 SQL 重贴即可；
- 结果标注「⚠️ 探索路径结果（非注册口径）」。

### 方式 B · 自然语言描述（NL）
用户用 NL 说诉求（"先看看华东区支付金额分布"）：
- 引擎识别 explore 意图 → 找表（metadata_search）→ **LLM 起草 SQL**
- 把 SQL 以代码块展示给用户 → 询问"确认执行 / 修改后执行"（ask_user_question）
- **用户确认后** → explore_validate → explore_execute
- ⚠️ 起草的 SQL 未确认前不得执行（返工代价高）；字段不确定先探查表结构

### 方式 C · 混合（推荐复杂分析）—— 触发词：起草 / 骨架 / 生成SQL

`intent_classify` 命中「起草 SQL/骨架」类词（起草、骨架、生成 SQL、写个 SQL、
我改改…）→ 返回 `explore_mode=mixed`。Agent 行为：
```
NL 诉求 + 起草信号（explore_mode=mixed）
  → 找表（metadata_search/traverse）→ LLM 起草 SQL 骨架
  → 代码块展示给用户 → ask_user_question："确认执行 / 修改后执行"
  → 用户可对话增量修改（"再加门店维度"、"按周聚合"）→ 改完再确认
  → explore_validate → explore_execute
```
用户不需要懂完整 SQL 结构，通过对话增量修改（"加个城市维度/按周聚合"）。
**触发示例**（直接在会话输入）：
- 「帮我起草个SQL看看华东区支付金额分布」
- 「先给我个SQL骨架我改改，看下订单和退款数据」
- 「生成SQL，探索一下门店维度的消费情况」

> `explore_mode` 三值：`direct`（贴 SQL，方式 A）/ `nl`（NL 描述，方式 B）/
> `mixed`（起草+精修，方式 C）。Agent 须按 mode 选择交互方式：
> mode=mixed 时**必须先展示骨架并等用户确认/修改，不得直接执行**。

## 触发条件（满足其一即进入）
1. `intent_classify` 返回 `intent=explore`（探索信号 + 指标缺失，或 **会话内 SQL 输入**）；
2. 取数缺指标/维度 → 已询问是否新建 → 用户**拒绝新建**但仍想先看数据（明确同意走探索）。

> 指标已注册 → 走 A（query-metric）正式取数；用户要新建 → 走 D（modeling-etl）。
> 探索是"未注册口径的先看数据"，不是绕过正式链的常态路径。

## 通用流程（方式 A/B/C 共用）——先确认再执行（硬性）

> **核心原则：探索 SQL 是"没指标自己写"，写错了执行就是返工。
> 除方式 A（用户自己贴的 SQL）外，任何由你起草的 SQL 都必须：
> 先展示草稿 → 用户确认/编辑 → 才执行。禁止起草后直接执行。**

1. **找表**（OAG 快速版，不做过度检索）：`metadata_search("<主题>")` 一次拿到
   候选事实表 + 关联维度表即可；不猜表名。**不要**反复调用 metric_disambiguate/
   ontology_search 逐个排查——指标未注册就直接探索，别在注册检索上绕圈。
2. **写/取 SQL**：
   - 方式 A：直接用用户贴的 SQL（用户已确认过，可直接执行）；
   - 方式 B/C：**起草 SQL → 以代码块展示给用户 → `ask_user_question` 请用户
     确认或指出要改的地方（列/条件/聚合/粒度）→ 用户确认或修改后再进入校验**。
   - 字段不确定时（如表结构未知），**先探查表结构再起草**：
     `explore_validate("SELECT * FROM <表> WHERE dt='<某天>' LIMIT 1")` → execute
     看 columns——避免写出不存在的列（如消费表没有 order_id）。
3. **校验**：`explore_validate(sql)`——只读 + 表名白名单 + 强制 dt 分区；
   失败按 errors 修正后**重新向用户展示修正版 SQL 并再次确认**（最多 3 轮）。
4. **执行观测**：用户确认的最终 SQL → `explore_execute(sql, token)` → 结果集
   （≤1000 行）；可反复修改重跑（观测迭代，看口径/数据是否符合预期）。
5. **固化（P2 桥接）**：观测稳定后 → `explore_promote(sql)` 自动提取口径草稿
   （聚合表达式→候选指标公式、GROUP BY→维度、WHERE→required_filters、
   源表→source_tables；物理列自动反查业务属性）：
   - 草稿为 `status=draft`（未激活），**必须经用户确认**（modeling_plan 清单确认）；
   - 确认后 `ontology_register` 固化（公式用业务属性名，翻译引擎自动映射回物理列）；
   - 未映射的物理列会列入 unmapped_columns（人工补 field_mapping，不臆造）。

## 禁止
- 禁止写库/DDL/任何非 SELECT 语句（explore_execute 已强制）；
- 禁止把物理表名/字段名作为「注册口径」告知用户（探索结果 = 非注册口径）；
- 禁止绕过 explore_validate 直接执行（explore_token 已强制绑定）；
- 禁止在用户未明确同意探索时自动走本路径（默认 query-metric / 结束）；
- **禁止起草后直接执行**：方式 B/C 的 SQL 未经用户确认/编辑，不得进入
  explore_execute（返工代价高；确认环节同时兜底口径与字段正确性）；
- **禁止对未注册指标做过度检索**：metric_disambiguate 只在用户疑似想要
  已注册指标族时用；探索场景确认无注册指标后立即转向找表+写 SQL。

## 结果展示规范（数据必须用表格，禁止文本罗列）

探索结果返回的数据部分**必须用 Markdown 表格**详细展示，禁止用逗号/斜杠文本罗列：

1. **完整表格**：用 `explore_execute` 返回的 `columns` 作表头，`rows` 逐行渲染；
   有多少行展示多少行（≤1000 行）；列较多时可分组展示但不可省略列值。
2. **表头注明**：`| 列名 | 列名 | ... |`，表头与数据之间必须有分隔行 `|---|---|`。
3. **行数说明**：表格上方注明「共 N 行」（引用返回的 `row_count`）；
   若 `truncated=true` 明确标注「已截断（仅前 1000 行）」。
4. **禁止自我统计**：不要自己数行数/算合计/总结分布后当结论——
   引用返回的 `row_count` 与 `rows`；确需观察分布（如状态计数）时，
   基于表格数据**逐条核对后再给结论**，数不清就只展示表格不总结。
5. **数值格式**：金额等数值保留返回原样（小数位不变），不加千分位改写。
6. **结尾标记**：表格后必须跟「⚠️ 探索路径结果（非注册口径），仅供观测」声明。

展示格式示例：
```
共 48 行（dt=20260817，未截断）

| order_id | user_id | product_id | store_id | region_id | order_date | order_amt | order_status | is_valid |
|---|---|---|---|---|---|---|---|---|
| O000005283 | U000001 | P0001 | S0002 | R01 | 2026-08-17 | 730.55 | completed | 1 |
| ...（逐行完整展示）... |

⚠️ 探索路径结果（非注册口径），仅供观测；稳定后走路径 D 固化注册。
```

## 输出标记
所有探索结果必须声明：
「⚠️ 探索路径结果（非注册口径），仅供观测；稳定后走路径 D 固化注册。」
