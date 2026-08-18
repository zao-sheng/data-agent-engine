# data-agent-engine：order 主题 Data Agent（DSH + Python）

基于 DeepSeek Harness 的数据查询 Agent 引擎：**OAG 理解 → MQL → 确定性翻译 → SQL 执行**。
零 JS 编写（引擎 100% Python），自带 order 主题数仓样例（15 张表，DWD/DWS/ADS/DIM 分层），
克隆即用。引擎按「理解与执行分离」设计：LLM 只负责把自然语言解析为 MQL，翻译引擎以
确定性规则生成 SQL 并执行，**不猜表名、不猜 JOIN、不猜过滤**。

## 快速开始（3 步）

```bash
git clone <repo> && cd data-agent-engine
./install.sh --sample        # 装环境 + 生成样例库 + 自动配置 DSH + 建「数据助理」预设
# 重启 DSH → 新开会话选「数据助理」→ 提问：
#   "上个月华东区活跃用户的平均客单价，按周拆分"
```

> 首次运行需手动 `cd ~/.dsh/profiles/web && pnpm install`（安装 dsh-mcp-client 依赖）。

## 能力

| 能力 | 说明 |
|------|------|
| 分层数仓模拟 | 15 张表：DWD（下单/支付/消费/退款明细）× 4、DWS 日汇总 × 4、ADS 应用汇总 × 3、DIM 维度 × 4；统一 `dt`（'YYYYMMDD'）分区 |
| 三层口径一致 | 样例库的 DWS/ADS 由 DWD 聚合生成（模拟真实 ETL），同指标跨表结果一致，评测可验证 |
| 多表关联聚合 | 如「华东区数码 GMV 按门店城市」→ 事实表 × dim_product × dim_store × dim_region 自动 JOIN（JOIN 键来自 Ontology relations，零猜测） |
| 表选择优化 | 预聚合表（ads/dws）优先；维度不覆盖时自动回落明细表；行级权限存在时自动排除无权限列的表 |
| 默认 t-1 | 未识别时间参数 → 默认查询昨天，回答自动标注 |
| 安全边界 | MQL 物理渗入检测（dwd_/dws_/pay_amt 等一律拒绝）；只读执行器（禁写、行数上限） |
| 评测门禁 | Golden Dataset 回归，通过率 ≥ 90% 才放行（CI 已配置） |

## 架构

```
DSH 侧（配置，零代码）                Python 侧（引擎）
┌──────────────────────────┐   MCP    ┌──────────────────────────────┐
│ 数据助理 preset           │ ──────▶ │ mcp_servers/server.py        │
│  persona + 3 个 skills    │  stdio   │  ├ ontology_search/traverse  │
│  dsh-mcp-client           │          │  ├ mql_validate（安全边界）  │
└──────────────────────────┘          │  ├ semantic_translate（翻译） │
                                      │  └ execute_sql（只读执行）    │
                                      └──────────────────────────────┘
```

```
backend/
├── seed/          样例数据生成器（schema.sql + seed.py，固定种子）
├── builder/       本体半自动构建器（表结构 → Ontology YAML 骨架）
├── ontology/      order 主题本体（objects/functions/relations）
├── core/          确定性引擎（loader / validator / translator / executor）
├── mcp_servers/   FastMCP 入口
└── eval/          Golden Dataset + 评测门禁
dsh-side/          setup_dsh.py + 「数据助理」预设模板
```

## 二次开发

### 换真实数仓
1. 配置连接：替换 `core/executor.py` 的 SQLite 实现为你的驱动（SQL 不变，方言在翻译引擎处理）；
2. 生成本体骨架：`uv run --project backend python -m builder.build_ontology --sqlite <库>`；
3. 🔴 人工补全：对象 `description`、指标 `formula`/口径/版本、relations `join_key` 核对；
4. 填 Golden Dataset 并跑门禁：`uv run --project backend python -m eval.eval`。

### 加指标
编辑 `backend/ontology/functions.yaml`（formula 只允许 SUM/COUNT/AVG/MAX/MIN/DISTINCT + 属性名），
并在 `objects.yaml` 对应事实表的 `pre_aggregated` 里声明预聚合列（如有）。

### 加表/对象
编辑 `objects.yaml`（source_tables 声明粒度/权威/joinable/available_dims/perm_column）+
`relations.yaml`（JOIN 键）→ 跑评测门禁。

### 行级权限
翻译引擎按 `user.region_ids` 注入 `F.region_id IN (...)`（步骤 0）。真实接入时：
从你的权限系统查出当前用户的可见区域 id，由会话上下文填入 `user` 参数即可。

### 多指标（MQL v1.1）
`metrics` 列表已完整支持：
- **同域多指标**（如 GMV + 客单价 + 支付笔数）→ 单表多列（全预聚合 或 全明细公式）；
- **跨域多指标**（如 GMV + 退款金额，分属 Payment / Refund）→ CTE + FULL OUTER JOIN 按共同维度对齐；无维度时返回单行标量（CROSS JOIN）；
- 无维度查询自动跨分区聚合（返回总和单行，而非逐日多行）。

## 文档

- [方案设计（修订版 v2.0）](DataAgent落地技术方案-修订版v2.0.md)
- [落地指南（Python 版）](DSH实现DataAgent落地指南-Python版.md)

## License

MIT
