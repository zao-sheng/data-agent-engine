"""翻译引擎：MQL → 可执行 SQL（确定性，零 LLM）。

职责：
  1. 权限过滤注入（步骤 0）
  2. 指标解析：公式 AST 白名单编译；预聚合表命中时直接取列
  3. 表选择：粒度覆盖 × 维度覆盖（直连/JOIN 扩展）× 预聚合 × 权威等级
  4. JOIN 链：唯一来源是 Ontology relations（翻译引擎永不猜测 JOIN）
  5. 时间：未识别时间参数 → 默认 t-1；相对表达式 → SQL；分区列统一 dt
  6. 方言：sqlite（默认，可执行）/ doris（映射）

设计约束：
  * 本版支持单指标（metrics 长度 1）；多指标返回明确错误（v1.1 预留）
  * 公式只允许白名单函数 + 属性名 + 运算符，非法标识符直接拒绝
"""
from __future__ import annotations

import re

from .ontology_loader import Ontology

FORMULA_FNS = {"SUM", "COUNT", "AVG", "MAX", "MIN", "DISTINCT"}
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
PHYSICAL_RE = re.compile(r"(?i)\b(dwd|dws|ads|dim|ods)_[a-z0-9_]+")

OP_SQL = {"eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=",
          "like": "LIKE"}


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
        metrics = mql.get("metrics") or ([mql["metric"]] if mql.get("metric") else None)
        if not metrics:
            raise TranslateError("MQL 缺少 metrics")
        if len(metrics) > 1:
            raise TranslateError("多指标查询（v1.1）尚未启用，请拆分为单指标查询")
        metric = metrics[0]["name"] if isinstance(metrics[0], dict) else metrics[0]
        fn = self.onto.get_function(metric)
        if fn is None:
            raise TranslateError(f"指标 {metric} 未注册")
        fact_obj = fn["owner"]
        fact_table, best_t, joins = self._plan(mql, fn, user)

        # ① 权限注入（步骤 0）
        perm = self._permission_clause(user, best_t)

        # ② 指标 SELECT 列
        select = [self._metric_select(fn, best_t, fact_table, fact_obj)]

        # ③ 维度列 + GROUP BY
        group_exprs: list[str] = []
        for d in mql.get("dimensions", []):
            expr, _alias = self._dim_expr(d, fact_obj, fact_table, best_t)
            select.append(f"{expr} AS {_alias}")
            group_exprs.append(expr)

        # ④ WHERE：required_filters + 权限 + 过滤 + 时间
        # 预聚合表（DWS/ADS）的指标已在 ETL 时应用过滤，不再注入 required_filter
        where = []
        if best_t.get("layer") == "DWD":
            where += [self._map_required(f, fact_table, fact_obj)
                      for f in fn.get("required_filters", [])]
        where += [self._filter_sql(f, fact_obj, fact_table, best_t) for f in mql.get("filters", [])]
        if perm:
            where.append(perm)
        time_sql, time_desc = self._time_sql(mql.get("time_range"), dialect)
        where.append(time_sql)

        # ⑤ 组装
        sql = f"SELECT {', '.join(select)} FROM {fact_table} F"
        if joins:
            sql += " " + " ".join(joins)
        sql += f" WHERE {' AND '.join(w for w in where if w)}"
        if group_exprs:
            sql += f" GROUP BY {', '.join(group_exprs)}"
        sql += self._order_limit(mql, metric)

        injected = list(fn.get("required_filters", [])) + [time_desc]
        if perm:
            injected.append("行级权限过滤")
        return {
            "sql": sql,
            "metadata": {
                "metric": metric,
                "metric_version": fn.get("version", "v1.0"),
                "tables_used": [fact_table] + [j.split(" ")[1] for j in joins],
                "fact_table": fact_table,
                "pre_aggregated": metric in best_t.get("pre_aggregated", {}),
                "injected_filters": injected,
                "time_range": time_desc,
                "granularity": self._granularity(mql),
                "dialect": dialect,
                "formula": fn["formula"],
            },
        }

    # ── 表选择 ──────────────────────────────────────────────
    def _granularity(self, mql: dict) -> str:
        for d in mql.get("dimensions", []):
            if d.get("name") == "order_date":
                return d.get("granularity", "day")
        return "day"

    def _gran_ok(self, t: dict, gran: str) -> bool:
        gs = t.get("granularities", [])
        return gran in gs or ("day" in gs and gran in ("week", "month", "quarter", "year"))

    def _plan(self, mql: dict, fn: dict, user: dict) -> tuple[str, dict, list[str]]:
        fact_obj = fn["owner"]
        gran = self._granularity(mql)
        needed = ({d["name"] for d in mql.get("dimensions", [])}
                  | {f["field"] for f in mql.get("filters", [])}) - {"order_date"}
        perm_rids = user.get("region_ids")

        cands = []
        for t in self.onto.get_object(fact_obj)["source_tables"]:
            if not self._gran_ok(t, gran):
                continue
            # 行级权限：表必须声明权限维度列（ads_ord_gmv_1d 无 region_id → 有权限时排除）
            if perm_rids and not t.get("perm_column"):
                continue
            # 非预聚合指标必须落在明细表（公式列只在明细表可映射）
            if fn["name"] not in t.get("pre_aggregated", {}) and not t.get("joinable"):
                continue
            direct = needed <= set(t.get("available_dims", []))
            joinable = (t.get("joinable") and bool(needed) and
                        all(self.onto.reachable(fact_obj, self.onto.owner_of(p)) for p in needed))
            if direct or joinable:
                cands.append((t, direct, joinable))

        if not cands:
            raise TranslateError(f"无可覆盖该查询的表（指标 {fn['name']}，粒度 {gran}，"
                                 f"维度 {sorted(needed)}）。请走降级链或补充 Ontology 映射。")

        def score(c):
            t, direct, _j = c
            s = 0
            if fn["name"] in t.get("pre_aggregated", {}):
                s += 100                      # 预聚合列（性能最优）
            elif direct:
                s += 60
            if t.get("authority") == "gold":
                s += 20
            if t.get("layer") in ("ADS", "DWS"):
                s += 10                       # 汇总表优先于明细（成本）
            return s

        best_t, direct, joinable = max(cands, key=score)
        joins = self._build_joins(fact_obj, best_t, needed) if joinable else []
        return best_t["table"], best_t, joins

    def _build_joins(self, fact_obj: str, best_t: dict, needed: set[str]) -> list[str]:
        """沿 relations BFS 组装 JOIN 链（join_key 一律来自 Ontology）"""
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
    def _metric_select(self, fn: dict, t: dict, fact_table: str, fact_obj: str) -> str:
        pre = t.get("pre_aggregated", {})
        if fn["name"] in pre:
            col = pre[fn["name"]]
            return f"F.{col} AS {fn['name']}"
        # 公式编译：属性 → 事实表列
        cols = {p: self.onto.column_of(fact_obj, fact_table, p)
                for p in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", fn["formula"])
                if p in self.onto.property_owner}
        return f"({self._compile_formula(fn['formula'], cols)}) AS {fn['name']}"

    def _compile_formula(self, formula: str, cols: dict[str, str]) -> str:
        for prop, col in cols.items():
            formula = re.sub(rf"\b{re.escape(prop)}\b", f"F.{col}", formula)
        # 白名单检查：先移除已映射的 F.xxx 列引用，剩余标识符只允许白名单函数
        cleaned = re.sub(r"F\.[A-Za-z_][A-Za-z0-9_]*", "", formula)
        for tok in IDENT_RE.findall(cleaned):
            if tok not in FORMULA_FNS:
                raise TranslateError(f"公式含非法标识符: {tok}")
        if PHYSICAL_RE.search(formula):
            raise TranslateError("公式中出现物理表名")
        return formula

    def _dim_expr(self, d: dict, fact_obj: str, fact_table: str, best_t: dict) -> tuple[str, str]:
        name = d["name"]
        if name == "order_date":
            return self._time_group_expr(d.get("granularity", "day")), "order_date"
        owner = self.onto.owner_of(name)
        if owner is None:
            raise TranslateError(f"维度 {name} 不是注册属性")
        if owner == fact_obj or name in best_t.get("available_dims", []):
            col = self.onto.column_of(fact_obj, fact_table, name)
            return f"F.{col}", name
        # 维度表列
        d_table = self.onto.dim_table_of(owner)
        col = self.onto.column_of(owner, d_table, name)
        return f"{self.onto.alias_of(owner)}.{col}", name

    # ── WHERE ───────────────────────────────────────────────
    def _map_required(self, cond: str, fact_table: str, fact_obj: str) -> str:
        """required_filter（如 "is_valid = 1"）→ "F.is_valid = 1"（物理字段统一加 F. 前缀）"""
        out = cond
        # 按长度降序替换，避免公共前缀误伤（is_valid vs is_valid_flag）
        tokens = sorted(set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", cond)), key=len, reverse=True)
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
    def _time_sql(self, time_range, dialect: str) -> tuple[str, str]:
        alias = "F"
        if not time_range:                                    # 未识别时间参数 → 默认 t-1
            return (f"{alias}.dt = {self._expr('yesterday', dialect)}", "t-1（昨天）")
        if isinstance(time_range, str):
            return (f"{alias}.dt = {self._expr(time_range, dialect)}", f"单日 {time_range}")
        if "day" in time_range:
            return (f"{alias}.dt = {self._expr(time_range['day'], dialect)}",
                    f"单日 {time_range['day']}")
        start, end = time_range.get("start"), time_range.get("end")
        if start and end:
            return (f"{alias}.dt >= {self._expr(start, dialect)} AND {alias}.dt <= {self._expr(end, dialect)}",
                    f"{start} ~ {end}")
        if start:
            return (f"{alias}.dt >= {self._expr(start, dialect)}", f">= {start}")
        if end:
            return (f"{alias}.dt <= {self._expr(end, dialect)}", f"<= {end}")
        return (f"{alias}.dt = {self._expr('yesterday', dialect)}", "t-1（昨天）")

    @staticmethod
    def _expr(x, dialect: str) -> str:
        """相对/绝对时间表达式 → SQL（分区 dt 为 'YYYYMMDD'）"""
        if dialect == "doris":
            rel = {"today": "DATE_FORMAT(CURDATE(),'%Y%m%d')",
                   "yesterday": "DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 1 DAY),'%Y%m%d')",
                   "last_month_start": "DATE_FORMAT(DATE_SUB(DATE_FORMAT(CURDATE(),'%Y-%m-01'), INTERVAL 1 MONTH),'%Y%m%d')",
                   "last_month_end": "DATE_FORMAT(LAST_DAY(DATE_SUB(CURDATE(), INTERVAL 1 MONTH)),'%Y%m%d')"}
            if x in rel:
                return rel[x]
            m = re.fullmatch(r"-(\d+)d", x)
            if m:
                return f"DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL {m.group(1)} DAY),'%Y%m%d')"
        else:
            rel = {"today": "strftime('%Y%m%d','now')",
                   "yesterday": "strftime('%Y%m%d', date('now','-1 day'))",
                   "last_month_start": "strftime('%Y%m%d', date('now','start of month','-1 month'))",
                   "last_month_end": "strftime('%Y%m%d', date('now','start of month','-1 day'))"}
            if x in rel:
                return rel[x]
            m = re.fullmatch(r"-(\d+)d", x)
            if m:
                return f"strftime('%Y%m%d', date('now','-{m.group(1)} days'))"
        # 绝对日期：'YYYY-MM-DD' 或 'YYYYMMDD' → 字面量
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", x):
            return f"'{x.replace('-', '')}'"
        if re.fullmatch(r"\d{8}", x):
            return f"'{x}'"
        raise TranslateError(f"无法解析时间表达式: {x}")

    def _time_group_expr(self, gran: str, dialect: str = "sqlite") -> str:
        dt = "F.dt"   # 分区列（'YYYYMMDD'，紧凑格式：strftime 无法解析，用字符串运算）
        if dialect == "doris":
            return {"day": dt,
                    "week": "DATE_FORMAT(F.dt,'%x-W%v')",
                    "month": "DATE_FORMAT(F.dt,'%Y-%m')",
                    "quarter": "CONCAT(SUBSTR(F.dt,1,4),'-Q',CEIL(CAST(SUBSTR(F.dt,5,2) AS INT)/3))",
                    "year": "SUBSTR(F.dt,1,4)"}[gran]
        return {"day": dt,
                "week": f"substr({dt},1,4) || '-W' || printf('%02d', iso_week({dt}))",
                "month": f"substr({dt},1,4) || '-' || substr({dt},5,2)",
                "quarter": f"substr({dt},1,4) || '-Q' || ((CAST(substr({dt},5,2) AS INTEGER) + 2) / 3)",
                "year": f"substr({dt},1,4)"}[gran]

    # ── 排序/限量 ───────────────────────────────────────────
    def _order_limit(self, mql: dict, metric: str) -> str:
        sql = ""
        for s in mql.get("sort", []):
            if s["field"] == metric or s["field"] in {d["name"] for d in mql.get("dimensions", [])}:
                sql += f" ORDER BY {s['field']} {s.get('order', 'asc').upper()}"
                break
        if mql.get("limit"):
            sql += f" LIMIT {int(mql['limit'])}"
        return sql


def _lit(v) -> str:
    """值 → SQL 字面量（字符串加引号并转义；数字原样）"""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    return f"'{str(v).replace(chr(39), chr(39) * 2)}'"
