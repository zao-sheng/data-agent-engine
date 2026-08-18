---
name: oag-retrieval
description: OAG 检索增强五步。用户查询数据指标前必须先执行本流程，再生成 MQL。
---

# OAG 检索增强（五步 + Step 0 术语归一）

查询任何数据前，按顺序执行；每一步的输入只能来自 MCP 工具返回，不得编造。

## Step 0 · 术语归一（业务黑话检测，先于一切）
`mcp__dataagent__term_normalize("<用户原句>")` 把业务黑话/别名归一为 Ontology 标准术语：
- 返回 `normalized_text`（用于后续实体识别）+ `mappings`（原始词 → 标准术语）；
- 词典未覆盖的黑话 → 用你的语义理解对照 Ontology 描述推断，**命中候选后在确认环节展示**；
- **回答回译**：最终回答用词跟随用户（用户说"店铺/poi"就用"店铺"，归一映射仅内部用）。

## Step 1 · 实体识别
从**归一后的**用户问题提取业务实体与指标，用 `mcp__dataagent__ontology_search` 逐一确认。
- 只接受 Ontology 中已注册的对象/指标/属性名（含中文 display_name、别名）
- 命中多个 → 保留候选列表；零命中 → 触发澄清（不要发明）
- 指标疑似多口径（GMV 家族）→ 用 `mcp__dataagent__metric_disambiguate` 判定
  （精确/族默认/召回确认，规则见 mql-authoring skill）

## Step 2 · 对象定位（含版本解析）
`mcp__dataagent__ontology_search("<对象名>")` 拿到对象定义后：
- 单版本 → 直接使用
- 多版本且有 default_version → 使用默认版本，回答时标注 `metric_version`
- 多版本无默认 → **必须反问用户**（列出版本差异），绝不静默选最新

## Step 3 · 关系图扩展（核心）
`mcp__dataagent__ontology_traverse("<事实对象>", 2)` 获取：
- JOIN 键（join_key）——后续翻译引擎会用，你不要自己猜 JOIN
- 必要过滤（required_filters，如 is_valid=1）
- 指标公式（Function definition）
- 可达维度对象（产品/门店/区域/用户）

## Step 4 · 精准文档
无独立文档检索工具时，跳过并注明；P1 接入 FAQ 文档后再启用。

## Step 5 · 组装并生成 MQL
按 `mql-authoring` 技能生成 MQL → **必须** `mcp__dataagent__mql_validate`。
校验失败 → 按错误修正后重验（最多 2 次）→ 仍失败则反问用户。
