"""MQL 领域常量（共享）：翻译引擎/校验器/ETL 生成器/条目校验器共用。

目的：消除 FORMULA_FNS / PHYSICAL_RE / OPERATORS / GRANULARITIES 等常量
在多个模块重复定义导致的「口径漂移」风险——一处定义，处处引用。
"""
from __future__ import annotations

import re

# ── 指标公式白名单函数（translator / etl_gen / entry_validator 共用）──
FORMULA_FNS = {"SUM", "COUNT", "AVG", "MAX", "MIN", "DISTINCT"}

# ── MQL 过滤操作符（validator 校验 / translator 映射共用）────────────
OPERATORS = {"eq", "neq", "gt", "gte", "lt", "lte", "in", "not_in", "like", "between"}
OP_SQL = {"eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=",
          "like": "LIKE"}

# ── 时间粒度（validator 校验 / translator 粒度表达式共用）─────────────
GRANULARITIES = {"day", "week", "month", "quarter", "year"}

# ── 物理标识符检测（validator / translator 的物理渗入与公式编译共用）──
# 物理表名特征（真实数仓的前缀约定，兜底强信号）
PHYSICAL_RE = re.compile(r"(?i)\b(dwd|dws|ads|dim|ods)_[a-z0-9_]+")
# 标识符 token（公式/条件里的属性名、函数名）
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# 相对时间表达式：^-Nd$（如 -7d）
REL_RE = re.compile(r"^-(\d+)d$")
