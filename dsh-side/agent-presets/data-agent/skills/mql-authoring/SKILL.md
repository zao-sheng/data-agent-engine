---
name: mql-authoring
description: MQL v1.1 编写规范。生成 MQL 时遵守本规范（结构、命名、时间默认、版本标注）。
---

# MQL 编写规范

## 结构（MQL v1.1）
```yaml
metrics:
  - {name: <已注册指标>, alias: 可选}
dimensions:
  - {name: <Ontology 属性名>, granularity: 可选(时间维度 day/week/month/quarter/year)}
filters:
  - {field: <Ontology 属性名>, operator: eq|neq|gt|gte|lt|lte|in|not_in|like|between, value: ...}
time_range: {start: 相对表达式|日期, end: ...}   # 或 {day: ...}
sort: [{field: <指标名或维度名>, order: asc|desc}]
limit: 整数
```

## 硬性规则
1. **纯业务语义**：metrics/dimensions/filters 只能使用 Ontology 中已注册的名字。
   出现 `dwd_/dws_/ads_/dim_` 前缀或 `pay_amt`/`order_cnt` 等物理列名 = schema 错误。
2. **时间**：用户未给时间 → 不写 time_range（翻译引擎按 **t-1** 处理，并在回答标注）；
   给了时间 → 用相对表达式（`-7d`、`today`、`yesterday`、`last_month_start`/`last_month_end`）
   或具体日期 `YYYY-MM-DD`。
3. **时间维度**：按周/月/季度拆分时用 `dimensions: [{name: order_date, granularity: week}]`。
4. **指标口径**：metric 必须是 functions.yaml 已注册的名字；不确定口径时查
   `ontology_search` 的描述与 do_not，仍不确定则反问。
5. **版本标注**：涉及多版本对象（如活跃用户）时，在最终回答中标注所用版本。

## 反例（禁止）
```yaml
# ❌ 物理渗入：dwd_ord_pay_di 是表名，pay_amt 是物理列
metrics: [{name: gmv}]
filters: [{field: dwd_ord_pay_di.pay_amt, operator: gt, value: 100}]
```
```yaml
# ✅ 正确写法
metrics: [{name: gmv}]
filters: [{field: pay_amount, operator: gt, value: 100}]
```

## 多指标（MQL v1.1，已支持）
```yaml
metrics:
  - {name: gmv}
  - {name: avg_order_amount}
  - {name: pay_count}
```
- **同域多指标**（如 GMV + 客单价，都属支付）→ 引擎自动合并到单表多列；
- **跨域多指标**（如 GMV + 退款金额，分属支付/退款）→ 引擎自动 CTE + FULL JOIN 对齐共同维度；
- 无维度时返回单行总和（自动跨分区聚合）。
