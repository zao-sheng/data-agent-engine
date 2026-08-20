"""翻译引擎：MQL → 可执行 SQL（确定性，零 LLM）。

职责：
  1. 权限过滤注入（步骤 0）
  2. 指标解析：公式 AST 白名单编译；预聚合表命中时直接取列
  3. 表选择：粒度覆盖 × 维度覆盖（直连/JOIN 扩展）× 预聚合 × 权威等级
  4. JOIN 链：唯一来源是 Ontology relations（翻译引擎永不猜测 JOIN）
  5. 时间：未识别时间参数 → 默认 t-1；相对表达式 → SQL；分区列统一 dt
  6. 方言：sqlite（默认，可执行）/ mysql / doris / hive / sparksql
     （远程方言翻译已映射，执行需 Executor 接入对应驱动 + .env 连接串）
  7. 多指标（MQL v1.1）：
     * 同 owner 多指标 → 单表多列（全预聚合 或 全明细公式）
     * 跨 owner 多指标 → CTE + FULL OUTER JOIN 按共同维度对齐（无维度 → CROSS JOIN 标量）

设计约束：
  * 公式只允许白名单函数 + 属性名 + 运算符，非法标识符直接拒绝
"""
from __future__ import annotations

import re

from .mql_schema import FORMULA_FNS, IDENT_RE, OP_SQL, PHYSICAL_RE, REL_RE
from .ontology_loader import Ontology

GRAN_UP = {"week", "month", "quarter", "year"}

# 方言验证状态：sqlite 已实现且评测覆盖（含真实执行）；
# 远程方言（mysql/doris/hive/sparksql）翻译已映射并由快照测试锁定，
# 但执行需在真实引擎回归（dialect_verified=False，接入驱动后逐个点亮）。
DIALECT_VERIFIED = {"sqlite": True, "mysql": False, "doris": False,
                    "hive": False, "sparksql": False}

SUPPORTED_DIALECTS = set(DIALECT_VERIFIED)

# ── 方言表达式注册表（P1-5）──────────────────────────────
# 时间表达式：t0/today/yesterday/last_month_start/last_month_end/相对 N 天
# 说明：hive/sparksql 的日期函数与 mysql 系不同，单独映射。
TIME_EXPRS: dict[str, dict[str, str]] = {
    "sqlite": {
        "today": "strftime('%Y%m%d','now')",
        "yesterday": "strftime('%Y%m%d', date('now','-1 day'))",
        "last_month_start": "strftime('%Y%m%d', date('now','start of month','-1 month'))",
        "last_month_end": "strftime('%Y%m%d', date('now','start of month','-1 day'))",
        "rel": "strftime('%Y%m%d', date('now','-{n} days'))",
    },
    "mysql": {
        "today": "DATE_FORMAT(CURDATE(),'%Y%m%d')",
        "yesterday": "DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 1 DAY),'%Y%m%d')",
        "last_month_start": "DATE_FORMAT(DATE_SUB(DATE_FORMAT(CURDATE(),'%Y-%m-01'), INTERVAL 1 MONTH),'%Y%m%d')",
        "last_month_end": "DATE_FORMAT(LAST_DAY(DATE_SUB(CURDATE(), INTERVAL 1 MONTH)),'%Y%m%d')",
        "rel": "DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL {n} DAY),'%Y%m%d')",
    },
    "doris": {
        # Doris 兼容 MySQL 日期函数（CURDATE/DATE_SUB/DATE_FORMAT/LAST_DAY）
        "today": "DATE_FORMAT(CURDATE(),'%Y%m%d')",
        "yesterday": "DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 1 DAY),'%Y%m%d')",
        "last_month_start": "DATE_FORMAT(DATE_SUB(DATE_FORMAT(CURDATE(),'%Y-%m-01'), INTERVAL 1 MONTH),'%Y%m%d')",
        "last_month_end": "DATE_FORMAT(LAST_DAY(DATE_SUB(CURDATE(), INTERVAL 1 MONTH)),'%Y%m%d')",
        "rel": "DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL {n} DAY),'%Y%m%d')",
    },
    "hive": {
        "today": "FROM_UNIXTIME(UNIX_TIMESTAMP(),'yyyyMMdd')",
        "yesterday": "FROM_UNIXTIME(UNIX_TIMESTAMP() - 86400,'yyyyMMdd')",
        "last_month_start": "DATE_FORMAT(ADD_MONTHS(TRUNC(CURRENT_DATE,'MM'), -1),'yyyyMMdd')",
        "last_month_end": "DATE_FORMAT(LAST_DAY(ADD_MONTHS(TRUNC(CURRENT_DATE,'MM'), -1)),'yyyyMMdd')",
        "rel": "FROM_UNIXTIME(UNIX_TIMESTAMP() - {n} * 86400,'yyyyMMdd')",
    },
    "sparksql": {
        "today": "DATE_FORMAT(CURRENT_DATE,'yyyyMMdd')",
        "yesterday": "DATE_FORMAT(DATE_SUB(CURRENT_DATE, 1),'yyyyMMdd')",
        "last_month_start": "DATE_FORMAT(ADD_MONTHS(TRUNC(CURRENT_DATE,'MM'), -1),'yyyyMMdd')",
        "last_month_end": "DATE_FORMAT(LAST_DAY(ADD_MONTHS(TRUNC(CURRENT_DATE,'MM'), -1)),'yyyyMMdd')",
        "rel": "DATE_FORMAT(DATE_SUB(CURRENT_DATE, {n}),'yyyyMMdd')",
    },
}

# 时间粒度分组表达式：{gran: 表达式模板}（{dt} 为分区列）
GRAN_EXPRS: dict[str, dict[str, str]] = {
    "sqlite": {
        "day": "{dt}",
        "week": "substr({dt},1,4) || '-W' || printf('%02d', iso_week({dt}))",
        "month": "substr({dt},1,4) || '-' || substr({dt},5,2)",
        "quarter": "substr({dt},1,4) || '-Q' || ((CAST(substr({dt},5,2) AS INTEGER) + 2) / 3)",
        "year": "substr({dt},1,4)",
    },
    "mysql": {
        "day": "{dt}",
        "week": "DATE_FORMAT({dt},'%x-W%v')",
        "month": "DATE_FORMAT({dt},'%Y-%m')",
        "quarter": "CONCAT(SUBSTR({dt},1,4),'-Q',CEIL(CAST(SUBSTR({dt},5,2) AS UNSIGNED)/3))",
        "year": "SUBSTR({dt},1,4)",
    },
    "doris": {
        "day": "{dt}",
        "week": "DATE_FORMAT({dt},'%x-W%v')",
        "month": "DATE_FORMAT({dt},'%Y-%m')",
        "quarter": "CONCAT(SUBSTR({dt},1,4),'-Q',CEIL(CAST(SUBSTR({dt},5,2) AS INT)/3))",
        "year": "SUBSTR({dt},1,4)",
    },
    "hive": {
        "day": "{dt}",
        "week": "CONCAT(SUBSTR({dt},1,4),'-W',LPAD(WEEKOFYEAR({dt}),2,'0'))",
        "month": "SUBSTR({dt},1,7)",
        "quarter": "CONCAT(SUBSTR({dt},1,4),'-Q',CEIL(CAST(SUBSTR({dt},5,2) AS INT)/3))",
        "year": "SUBSTR({dt},1,4)",
    },
    "sparksql": {
        "day": "{dt}",
        "week": "CONCAT(SUBSTR({dt},1,4),'-W',LPAD(WEEKOFYEAR({dt}),2,'0'))",
        "month": "SUBSTR({dt},1,7)",
        "quarter": "CONCAT(SUBSTR({dt},1,4),'-Q',CEIL(CAST(SUBSTR({dt},5,2) AS INT)/3))",
        "year": "SUBSTR({dt},1,4)",
    },
}


class TranslateError(Exception):
    pass


class Translator:
    def __init__(self, ontology: Ontology):
        self.onto = ontology

    # ── 主入口 ──────────────────────────────────────────────
    def translate(self, mql: dict, user: dict | None = None, dialect: str = "sqlite") -> dict:
        try:
            return self._translate(mql, user or {}, dialect)
        except TranslateError as e:
            return {"error": str(e)}
        except KeyError as e:
            return {"error": f"Ontology 映射缺失: {e}"}

    def _translate(self, mql: dict, user: dict, dialect: str) -> dict:
        metric_items = self._metric_items(mql)
        # 按归属对象分组（同 owner 合并到一张表，跨 owner 走 CTE 合并）
        groups: dict[str, list[tuple[dict, dict]]] = {}
        for m in metric_items:
            fn = self.onto.get_function(m["name"])
            if fn is None:
                raise TranslateError(f"指标 {m['name']} 未注册")
            groups.setdefault(fn["owner"], []).append((m, fn))

        dims = mql.get("dimensions", [])
        filters = mql.get("filters", [])
        dim_names = [d["name"] for d in dims]

        if len(groups) == 1:
            owner = next(iter(groups))
            sql, meta = self._single_table_sql(owner, groups[owner], mql, user, dialect)
        else:
            sql, meta = self._multi_table_sql(groups, mql, user, dialect)

        meta.update({
            "metrics": [m["name"] for m in metric_items],
            "metric_versions": [self.onto.get_function(m["name"]).get("version", "v1.0")
                                for m in metric_items],
            "time_range": self._time_desc(mql.get("time_range")),
            "granularity": self._granularity(mql),
            "dialect": dialect,
            "dialect_verified": DIALECT_VERIFIED.get(dialect, False),
        })
        return {"sql": sql, "metadata": meta}

    @staticmethod
    def _metric_items(mql: dict) -> list[dict]:
        metrics = mql.get("metrics")
        if metrics:
            return [m if isinstance(m, dict) else {"name": m} for m in metrics]
        if mql.get("metric"):
            return [{"name": mql["metric"]}]
        raise TranslateError("MQL 缺少 metrics")

    # ── 同 owner：单表多列 ──────────────────────────────────
    def _single_table_sql(self, owner: str, items: list[tuple[dict, dict]],
                          mql: dict, user: dict, dialect: str,
                          include_order: bool = True) -> tuple[str, dict]:
        metric_names = [m["name"] for m, _ in items]
        gran = self._granularity(mql)
        needed = ({d["name"] for d in mql.get("dimensions", [])}
                  | {f["field"] for f in mql.get("filters", [])}) - {self.onto.time_dim}
        perm_rids = user.get("region_ids")

        best_t, mode, joins = self._select_table_multi(owner, metric_names, needed, gran, perm_rids)
        fact_table = best_t["table"]
        has_group = bool(mql.get("dimensions"))

        # SELECT：预聚合列 或 公式编译（无维度时预聚合列要跨分区聚合）
        select = self._metric_selects(items, best_t, owner, fact_table, mode,
                                      aggregate_preagg=not has_group)

        # 维度列 + GROUP BY
        group_exprs: list[str] = []
        for d in mql.get("dimensions", []):
            expr, alias = self._dim_expr(d, owner, fact_table, best_t, dialect)
            select.append(f"{expr} AS {alias}")
            group_exprs.append(expr)

        # WHERE
        where: list[str] = []
        if mode == "detail":   # 明细表才注入 required_filter（预聚合表已在 ETL 应用）
            where += [self._map_required(f, fact_table, owner)
                      for f in self._owner_required(owner, items)]
        where += [self._filter_sql(f, owner, fact_table, best_t) for f in mql.get("filters", [])]
        perm = self._permission_clause(user, best_t)
        if perm:
            where.append(perm)
        where.append(self._time_sql(mql.get("time_range"), dialect))

        sql = f"SELECT {', '.join(select)} FROM {fact_table} F"
        if joins:
            sql += " " + " ".join(joins)
        sql += f" WHERE {' AND '.join(w for w in where if w)}"
        if group_exprs:
            sql += f" GROUP BY {', '.join(group_exprs)}"
        if include_order:
            sql += self._order_limit(mql, {m["name"] for m, _ in items})

        meta = {
            "fact_table": fact_table,
            "tables_used": [fact_table] + [j.split(" ")[1] for j in joins],
            "pre_aggregated": mode == "preagg",
            "injected_filters": ([f"required_filter({self._owner_required(owner, items)})"]
                                 if mode == "detail" else []) + [self._time_desc(mql.get("time_range"))],
        }
        return sql, meta

    @staticmethod
    def _owner_required(owner: str, items: list[tuple[dict, dict]]) -> list[str]:
        """同 owner 多指标的 required_filters 并集（去重保序）"""
        seen, out = set(), []
        for _, fn in items:
            for f in fn.get("required_filters", []):
                if f not in seen:
                    seen.add(f)
                    out.append(f)
        return out

    def _select_table_multi(self, owner: str, metric_names: list[str], needed: set[str],
                            gran: str, perm_rids) -> tuple[dict, str, list[str]]:
        cands_preagg, cands_detail = [], []
        for t in self.onto.get_object(owner)["source_tables"]:
            if not self._gran_ok(t, gran):
                continue
            if perm_rids and not t.get("perm_column"):
                continue
            # 状态路由：默认只选 active 表；显式 deprecated 表仅在无替代时兜底
            if t.get("status") == "deprecated":
                continue
            direct = needed <= set(t.get("available_dims", []))
            joinable = (t.get("joinable") and bool(needed) and
                        all(self.onto.reachable(owner, self.onto.owner_of(p)) for p in needed))
            if not (direct or joinable):
                continue
            pre = t.get("pre_aggregated", {})
            if all(m in pre for m in metric_names) and direct:
                cands_preagg.append(t)          # 预聚合表直接覆盖全部指标 + 维度直连
            elif t.get("joinable"):
                cands_detail.append(t)          # 明细表：公式编译全部指标
        if cands_preagg:
            t = min(cands_preagg, key=lambda x: (x["authority"] != "gold", x["layer"] not in ("ADS", "DWS")))
            return t, "preagg", []
        if cands_detail:
            t = min(cands_detail, key=lambda x: x["authority"] != "gold")
            return t, "detail", self._build_joins(owner, t, needed)
        raise TranslateError(f"无可覆盖该查询的表（指标 {metric_names}，粒度 {gran}，维度 {sorted(needed)}）。"
                             "请走降级链或补充 Ontology 映射。")

    def _metric_selects(self, items: list[tuple[dict, dict]], t: dict,
                        owner: str, fact_table: str, mode: str,
                        aggregate_preagg: bool = False) -> list[str]:
        if mode == "preagg":
            pre = t["pre_aggregated"]
            if aggregate_preagg:
                # 无维度：预聚合表按分区存，需跨分区求和
                return [f"SUM(F.{pre[m['name']]}) AS {m['name']}" for m, _ in items]
            return [f"F.{pre[m['name']]} AS {m['name']}" for m, _ in items]
        out = []
        fm = t.get("field_mapping", {})
        for m, fn in items:
            # 公式字段 → 所选事实表的物理列（查 field_mapping，不依赖全局属性注册）
            props = [p for p in IDENT_RE.findall(fn["formula"]) if p in fm]
            cols = {p: fm[p] for p in props}
            out.append(f"({self._compile_formula(fn['formula'], cols)}) AS {m['name']}")
        return out

    # ── 跨 owner：CTE + FULL OUTER JOIN ─────────────────────
    def _multi_table_sql(self, groups: dict[str, list[tuple[dict, dict]]],
                         mql: dict, user: dict, dialect: str) -> tuple[str, dict]:
        dim_names = [d["name"] for d in mql.get("dimensions", [])]
        base = dict(mql)
        base.pop("sort", None)
        base.pop("limit", None)

        ctes: list[str] = []
        tables_used: list[str] = []
        any_preagg = False
        for i, (owner, items) in enumerate(groups.items()):
            sub_sql, meta = self._single_table_sql(owner, items, base, user, dialect,
                                                   include_order=False)
            ctes.append(f"_m{i} AS ({sub_sql})")
            tables_used += meta["tables_used"]
            any_preagg = any_preagg or meta["pre_aggregated"]

        with_cte = "WITH " + ", ".join(ctes) + " "

        # 维度列（COALESCE 对齐）
        if dim_names:
            coalesce_dims = [f"COALESCE(_m0.{d}, {', '.join(f'_m{j}.{d}' for j in range(1, len(ctes)))}) AS {d}"
                             for d in dim_names]
            select_dims = ", ".join(coalesce_dims)
            metric_cols = ", ".join(f"_m{i}.{m['name']}" for i, (_, items) in enumerate(groups.items())
                                    for m, _ in items)
            # FULL OUTER JOIN：以 _m0 维度为锚，链式合并
            joined = "_m0"
            for j in range(1, len(ctes)):
                on = " AND ".join(f"{joined}.{d} = _m{j}.{d}" for d in dim_names)
                joined = f"({joined} FULL OUTER JOIN _m{j} ON {on})"
            sql = with_cte + f"SELECT {select_dims}, {metric_cols} FROM {joined}"
        else:
            # 无维度：各子查询单行标量 → CROSS JOIN
            metric_cols = ", ".join(f"_m{i}.{m['name']}" for i, (_, items) in enumerate(groups.items())
                                    for m, _ in items)
            sql = with_cte + f"SELECT {metric_cols} FROM " + \
                " CROSS JOIN ".join(f"_m{i}" for i in range(len(ctes)))

        sql += self._order_limit(mql, {m["name"] for items in groups.values() for m, _ in items})

        meta = {
            "fact_table": " | ".join(tables_used),
            "tables_used": tables_used,
            "pre_aggregated": any_preagg,
            "multi_table": True,
        }
        return sql, meta

    # ── 表/粒度/维度 ────────────────────────────────────────
    def _granularity(self, mql: dict) -> str:
        for d in mql.get("dimensions", []):
            if d.get("name") == self.onto.time_dim:
                return d.get("granularity", "day")
        return "day"

    def _gran_ok(self, t: dict, gran: str) -> bool:
        gs = t.get("granularities", [])
        return gran in gs or ("day" in gs and gran in GRAN_UP)

    def _build_joins(self, fact_obj: str, best_t: dict, needed: set[str]) -> list[str]:
        joins, joined = [], set()
        avail = set(best_t.get("available_dims", []))
        for p in sorted(needed):
            owner = self.onto.owner_of(p)
            if owner is None or owner == fact_obj or p in avail:
                continue
            path = self.onto.join_path(fact_obj, owner)
            if not path:
                continue
            for (src, dst, jk) in path:
                d_table = self.onto.dim_table_of(dst)
                if d_table in joined:
                    continue
                d_alias = self.onto.alias_of(dst)
                s_alias = "F" if src == fact_obj else self.onto.alias_of(src)
                joins.append(f"JOIN {d_table} {d_alias} ON {d_alias}.{jk} = {s_alias}.{jk}")
                joined.add(d_table)
        return joins

    # ── 列解析 ──────────────────────────────────────────────
    def _compile_formula(self, formula: str, cols: dict[str, str]) -> str:
        for prop, col in cols.items():
            formula = re.sub(rf"\b{re.escape(prop)}\b", f"F.{col}", formula)
        cleaned = re.sub(r"F\.[A-Za-z_][A-Za-z0-9_]*", "", formula)
        for tok in IDENT_RE.findall(cleaned):
            if tok not in FORMULA_FNS:
                raise TranslateError(f"公式含非法标识符: {tok}")
        if PHYSICAL_RE.search(formula):
            raise TranslateError("公式中出现物理表名")
        return formula

    def _dim_expr(self, d: dict, fact_obj: str, fact_table: str, best_t: dict,
                  dialect: str = "sqlite") -> tuple[str, str]:
        name = d["name"]
        if name == self.onto.time_dim:
            return self._time_group_expr(d.get("granularity", "day"), dialect), self.onto.time_dim
        owner = self.onto.owner_of(name)
        if owner is None:
            raise TranslateError(f"维度 {name} 不是注册属性")
        if owner == fact_obj or name in best_t.get("available_dims", []):
            col = self.onto.column_of(fact_obj, fact_table, name)
            return f"F.{col}", name
        d_table = self.onto.dim_table_of(owner)
        col = self.onto.column_of(owner, d_table, name)
        return f"{self.onto.alias_of(owner)}.{col}", name

    # ── WHERE ───────────────────────────────────────────────
    def _map_required(self, cond: str, fact_table: str, fact_obj: str) -> str:
        out = cond
        tokens = sorted(set(IDENT_RE.findall(cond)), key=len, reverse=True)
        for p in tokens:
            if p in FORMULA_FNS:
                continue
            col = self.onto.column_of(fact_obj, fact_table, p) if p in self.onto.property_owner else p
            out = re.sub(rf"\b{re.escape(p)}\b", f"F.{col}", out)
        return out

    def _filter_sql(self, f: dict, fact_obj: str, fact_table: str, best_t: dict) -> str:
        field, op, val = f["field"], f["operator"], f["value"]
        owner = self.onto.owner_of(field)
        if owner == fact_obj or field in best_t.get("available_dims", []):
            col = self.onto.column_of(fact_obj, fact_table, field)
            expr = f"F.{col}"
        else:
            d_table = self.onto.dim_table_of(owner)
            col = self.onto.column_of(owner, d_table, field)
            expr = f"{self.onto.alias_of(owner)}.{col}"
        return self._op_sql(expr, op, val)

    @staticmethod
    def _op_sql(expr: str, op: str, val) -> str:
        if op in ("in", "not_in"):
            items = ", ".join(_lit(v) for v in val)
            return f"{expr} {'IN' if op == 'in' else 'NOT IN'} ({items})"
        if op == "between":
            return f"{expr} BETWEEN {_lit(val[0])} AND {_lit(val[1])}"
        return f"{expr} {OP_SQL[op]} {_lit(val)}"

    def _permission_clause(self, user: dict, best_t: dict) -> str:
        rids = user.get("region_ids")
        if not rids or not best_t.get("perm_column"):
            return ""
        return f"F.{best_t['perm_column']} IN ({', '.join(_lit(r) for r in rids)})"

    # ── 时间 ────────────────────────────────────────────────
    def _time_sql(self, time_range, dialect: str) -> str:
        alias = "F"
        if not time_range:
            return f"{alias}.{self.onto.partition_col} = {self._expr('yesterday', dialect)}"
        if isinstance(time_range, str):
            return f"{alias}.{self.onto.partition_col} = {self._expr(time_range, dialect)}"
        if "day" in time_range:
            return f"{alias}.{self.onto.partition_col} = {self._expr(time_range['day'], dialect)}"
        start, end = time_range.get("start"), time_range.get("end")
        if start and end:
            return (f"{alias}.{self.onto.partition_col} >= {self._expr(start, dialect)} "
                    f"AND {alias}.{self.onto.partition_col} <= {self._expr(end, dialect)}")
        if start:
            return f"{alias}.{self.onto.partition_col} >= {self._expr(start, dialect)}"
        if end:
            return f"{alias}.{self.onto.partition_col} <= {self._expr(end, dialect)}"
        return f"{alias}.{self.onto.partition_col} = {self._expr('yesterday', dialect)}"

    def _time_desc(self, time_range) -> str:
        if not time_range:
            return "t-1（昨天）"
        if isinstance(time_range, str):
            return f"单日 {time_range}"
        if "day" in time_range:
            return f"单日 {time_range['day']}"
        s, e = time_range.get("start"), time_range.get("end")
        if s and e:
            return f"{s} ~ {e}"
        return f"{s or ''}{'~' if s and e else ''}{e or ''}"

    @classmethod
    def _expr(cls, x, dialect: str) -> str:
        table = TIME_EXPRS.get(dialect)
        if table is None:
            raise TranslateError(f"不支持的目标方言: {dialect}（支持 {sorted(SUPPORTED_DIALECTS)}）")
        if x in table:
            return table[x]
        m = REL_RE.match(x)
        if m:
            return table["rel"].format(n=m.group(1))
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", x):
            return f"'{x.replace('-', '')}'"
        if re.fullmatch(r"\d{8}", x):
            return f"'{x}'"
        raise TranslateError(f"无法解析时间表达式: {x}")

    def _time_group_expr(self, gran: str, dialect: str = "sqlite") -> str:
        table = GRAN_EXPRS.get(dialect)
        if table is None:
            raise TranslateError(f"不支持的目标方言: {dialect}（支持 {sorted(SUPPORTED_DIALECTS)}）")
        dt = f"F.{self.onto.partition_col}"
        if gran not in table:
            raise TranslateError(f"粒度 {gran} 在方言 {dialect} 无映射")
        return table[gran].format(dt=dt)

    # ── 排序/限量 ───────────────────────────────────────────
    def _order_limit(self, mql: dict, metric_names: set[str]) -> str:
        sql = ""
        dim_names = {d["name"] for d in mql.get("dimensions", [])}
        for s in mql.get("sort", []):
            if s["field"] in metric_names or s["field"] in dim_names:
                sql += f" ORDER BY {s['field']} {s.get('order', 'asc').upper()}"
                break
        if mql.get("limit"):
            sql += f" LIMIT {int(mql['limit'])}"
        return sql


def _lit(v) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    return f"'{str(v).replace(chr(39), chr(39) * 2)}'"
