"""Ontology：从原始本体数据构建索引（对象/函数/关系/属性归属/黑话/搜索）。

数据源由 OntologyStore 提供（YAML 默认 / SQLite 编译缓存 / Supabase 真源），
本类只负责「原始数据 → 索引」，与存储解耦（见 core/ontology_store.py）。

翻译引擎（JOIN 链）与 MQL 校验器（属性存在性）都以这里的索引为准。
"""
from __future__ import annotations

from collections import deque
from pathlib import Path

from .ontology_store import OntologyData, OntologyStore, YamlOntologyStore


class Ontology:
    def __init__(self, base: Path | str | None = None,
                 store: OntologyStore | None = None):
        """构造：base（YAML 目录，兼容旧调用）或 store（自定义存储）。

        二选一：给 base 用 YamlOntologyStore；给 store 用自定义实现（sqlite/supabase）。
        """
        if store is None:
            if base is None:
                raise ValueError("Ontology 需要 base 或 store 参数")
            store = YamlOntologyStore(base)
        data = store.load()
        self._build(data)

    def _build(self, data: OntologyData) -> None:
        """从原始本体数据构建全部索引。"""
        raw_objects = data.objects
        raw_functions = data.functions
        self.relations: list[dict] = data.relations
        cfg = data.config

        self.objects: dict[str, dict] = {}
        self.functions: dict[str, dict] = {}
        self.property_owner: dict[str, str] = {}   # 业务属性 → 归属对象
        self.edges: dict[str, list[tuple[str, str]]] = {}  # source → [(target, join_key)]
        # 关系语义索引：source → [(target, join_key, type, cardinality, desc)]，
        # 供 OAG traverse 展示业务语义（翻译引擎仍用 edges 的 join_key）
        self.relation_meta: dict[str, list[dict]] = {}

        self.time_dim: str = cfg.get("time_dimension", "order_date")
        self.partition_col: str = cfg.get("partition_column", "dt")

        # 业务域路由索引：domain → [对象名]（跨域查询按域过滤候选集）
        self.domain_index: dict[str, list[str]] = {}
        # 对象类型索引：fact/dim → [对象名]
        self.object_types: dict[str, list[str]] = {"fact": [], "dim": []}

        for o in raw_objects:
            name = o["name"]
            self.objects[name] = o
            for p in o.get("properties", []):
                self.property_owner[p["name"]] = name
            # 域路由：显式 domain 或从表名推断（dwd_<domain>_<subject>_di → domain）
            dom = o.get("domain") or self._infer_domain(o)
            o["domain"] = dom
            self.domain_index.setdefault(dom, []).append(name)
            # 对象类型：显式 object_type，否则按表层推断（DIM → dim，其余 → fact）
            otype = o.get("object_type") or ("dim" if o.get("source_tables", [{}])[0].get("layer") == "DIM" else "fact")
            o["object_type"] = otype
            self.object_types.setdefault(otype, []).append(name)
        for f in raw_functions:
            self.functions[f["name"]] = f
            # 指标域：显式 domain，否则跟随归属对象
            if not f.get("domain"):
                f["domain"] = self.objects.get(f.get("owner", "") , {}).get("domain", "unknown")
        for r in self.relations:
            self.edges.setdefault(r["source"], []).append((r["target"], r["join_key"]))
            self.relation_meta.setdefault(r["source"], []).append({
                "target": r["target"], "join_key": r["join_key"],
                "type": r.get("type", ""), "cardinality": r.get("cardinality", "N:1"),
                "description": r.get("description", "")})

        # 指标族索引：family -> {variants: [指标名], default: 默认变体}
        self.families: dict[str, dict] = {}
        for f in raw_functions:
            fam = f.get("family")
            if fam:
                entry = self.families.setdefault(fam, {"variants": [], "default": None})
                entry["variants"].append(f["name"])
                if f.get("default_of_family"):
                    entry["default"] = f["name"]

        # 业务黑话倒排索引：黑话(小写) -> {canonical, type, display}
        self.term_index: dict[str, dict] = {}
        gloss = data.glossary

        def display_of(canonical: str, t: str) -> str:
            if t == "object":
                return self.objects[canonical].get("display_name", canonical)
            return canonical  # property / metric 用标准名

        def add_term(term: str, canonical: str, t: str) -> None:
            key = term.strip().lower()
            disp = display_of(canonical, t)
            if not key or key == disp.lower() or key == canonical.lower():
                return  # 标准名自身不替换
            self.term_index.setdefault(key, {"canonical": canonical, "type": t, "display": disp})

        for g in gloss:
            add_term(g["term"], g["canonical"], g["type"])
        for o in raw_objects:
            for a in o.get("aliases", []):
                add_term(a, o["name"], "object")
            for p in o.get("properties", []):
                for a in p.get("aliases", []):
                    add_term(a, p["name"], "property")

        # 物理名集合（物理表名 + 物理列名），供 MQL 物理渗入检测用。
        self.physical_names: set[str] = set()
        for o in raw_objects:
            for t in o.get("source_tables", []):
                self.physical_names.add(t["table"])
                self.physical_names.update(t.get("field_mapping", {}).values())
                self.physical_names.update(t.get("pre_aggregated", {}).values())
        business_names = set(self.functions) | set(self.property_owner)
        self.physical_names -= business_names

        # 维度对象 → SQL 别名（主题无关，动态生成）
        self.dim_aliases: dict[str, str] = {}
        counters: dict[str, int] = {}
        for o in raw_objects:
            if o.get("object_type") != "dim":
                continue
            name = o["name"]
            base = name[0].upper()
            if base == "F":
                base = (name[1:2].upper() or "X")
            c = counters.get(base, 0) + 1
            counters[base] = c
            self.dim_aliases[name] = base if c == 1 else f"{base}{c}"

        # 搜索倒排索引：key=归一化 token，value=命中描述行
        self.search_index: dict[str, list[str]] = {}
        for o in raw_objects:
            name = o["name"]
            props = ", ".join(p["name"] for p in o.get("properties", []))
            otype = o.get("object_type", "fact")
            status = o.get("status", "active")
            dom = o.get("domain", "unknown")
            line = (f"[对象] {name}（{o.get('display_name')}）：{o.get('description','')}"
                    f"；类型: {otype}；域: {dom}；状态: {status}"
                    f"{'；标签: ' + ','.join(o.get('tags', [])) if o.get('tags') else ''}"
                    f"；属性: {props}")
            for key in {name, o.get("display_name", ""), *o.get("aliases", [])}:
                if key:
                    self._index_add(str(key).lower(), line)
        for f in raw_functions:
            line = (f"[指标] {f['name']}（{f.get('display_name')}）：{f.get('description','')}"
                    f"；公式: {f['formula']}；版本: {f.get('version')}"
                    f"；域: {f.get('domain', 'unknown')}"
                    f"{'；标签: ' + ','.join(f.get('tags', [])) if f.get('tags') else ''}")
            for key in {f["name"], f.get("display_name", "")}:
                if key:
                    self._index_add(str(key).lower(), line)
        for p in self.property_owner:
            self._index_add(p.lower(), f"[属性] {p}（归属 {self.owner_of(p)}）")

    def _infer_domain(self, o: dict) -> str:
        """从 source_tables 物理表名推断业务域（复用 table_naming 解析）。

        表名规范：{layer}_{domain}_{subject}_{粒度}（如 dwd_ord_order_di → ord）。
        取首个有规范前缀的表；无表返回 unknown。
        """
        from .table_naming import domain_of_table
        for t in o.get("source_tables", []):
            dom = domain_of_table(t.get("table", ""))
            if dom:
                return dom
        return "unknown"

    def _index_add(self, key: str, line: str) -> None:
        """往倒排索引添加一条命中（同 key 去重）。"""
        lst = self.search_index.setdefault(key, [])
        if line not in lst:
            lst.append(line)

    def search(self, query: str) -> list[str]:
        """倒排索引检索：精确 token 命中 + 前缀兜底，按命中数排序。"""
        q = query.strip().lower()
        if not q:
            return []
        exact = self.search_index.get(q, [])
        prefixed = []
        for key, lines in self.search_index.items():
            if key != q and key.startswith(q):
                prefixed.extend(lines)
        # 去重保序：精确命中优先，前缀次之
        seen, out = set(), []
        for line in exact + prefixed:
            if line not in seen:
                seen.add(line)
                out.append(line)
        return out

    # ── 基础查询 ────────────────────────────────────────────
    def get_object(self, name: str) -> dict | None:
        return self.objects.get(name)

    def get_function(self, name: str) -> dict | None:
        return self.functions.get(name)

    def owner_of(self, prop: str) -> str | None:
        return self.property_owner.get(prop)

    def all_properties(self) -> list[str]:
        return list(self.property_owner)

    # ── 业务域路由（跨域查询按域过滤候选集）─────────────────
    def domains(self) -> list[str]:
        """全部业务域（保持对象声明顺序）。"""
        return list(self.domain_index)

    def objects_by_domain(self, domain: str) -> list[str]:
        """某业务域下的对象名（域路由：跨域查询时只取该域候选）。"""
        return list(self.domain_index.get(domain, []))

    def objects_of_type(self, otype: str) -> list[str]:
        """某类型（fact/dim）的对象名。"""
        return list(self.object_types.get(otype, []))

    def domain_of(self, obj_name: str) -> str | None:
        o = self.objects.get(obj_name)
        return o.get("domain") if o else None

    def search_by_domain(self, query: str, domain: str | None = None) -> list[str]:
        """检索（可选按业务域过滤）：返回命中行，域过滤时只留该域对象/指标。"""
        hits = self.search(query)
        if not domain:
            return hits
        out = []
        for line in hits:
            if line.startswith("[指标]"):
                if f"；域: {domain}；" in line or f"；域: {domain}" == line.rsplit("；", 1)[-1]:
                    out.append(line)
            elif line.startswith("[对象]"):
                # 对象行域信息格式：；类型: ...；域: <domain>；状态:
                if f"；域: {domain}；" in line:
                    out.append(line)
        return out

    def resolve_version(self, obj_name: str, hint: str = "") -> str | None:
        """版本解析三级规则：单版本 / default_version / None（需反问，绝不静默选最新）"""
        o = self.objects.get(obj_name)
        if not o:
            return None
        versions = [v for v in o.get("versions", []) if v.get("status") == "active"]
        if len(versions) <= 1:
            return versions[0]["version"] if versions else None
        if o.get("default_version"):
            return o["default_version"]
        return None  # 多版本且无默认 → 调用方必须反问

    # ── 关系图 ──────────────────────────────────────────────
    def join_path(self, start: str, target: str) -> list[tuple[str, str, str]] | None:
        """BFS 最短路径，返回 [(hop_source, hop_target, join_key), ...]；不可达返回 None"""
        if start == target:
            return []
        q: deque = deque([(start, [])])
        visited = {start}
        while q:
            node, path = q.popleft()
            for (nxt, jk) in self.edges.get(node, []):
                if nxt in visited:
                    continue
                new_path = path + [(node, nxt, jk)]
                if nxt == target:
                    return new_path
                visited.add(nxt)
                q.append((nxt, new_path))
        return None

    def reachable(self, fact_obj: str, dim_obj: str | None) -> bool:
        return dim_obj is not None and (fact_obj == dim_obj or self.join_path(fact_obj, dim_obj) is not None)

    # ── 表相关 ──────────────────────────────────────────────
    def dim_table_of(self, obj_name: str) -> str:
        o = self.objects[obj_name]
        return o["source_tables"][0]["table"]

    def column_of(self, obj_name: str, table: str, prop: str) -> str:
        """业务属性 → 物理列（在指定表的 field_mapping 中）"""
        o = self.objects[obj_name]
        for t in o["source_tables"]:
            if t["table"] == table:
                fm = t.get("field_mapping", {})
                if prop in fm:
                    return fm[prop]
        raise KeyError(f"属性 {prop} 在表 {table} 无映射")

    def alias_of(self, obj_name: str) -> str:
        return self.dim_aliases.get(obj_name, obj_name[0].upper())

    # ── 指标族 ──────────────────────────────────────────────
    def family_variants(self, family: str) -> list[str]:
        return list(self.families.get(family, {}).get("variants", []))

    def family_default(self, family: str) -> str | None:
        return self.families.get(family, {}).get("default")

    # ── 术语归一 ─────────────────────────────────────────────
    def normalize_terms(self, text: str) -> tuple[str, list[dict]]:
        """业务黑话/别名 → 标准展示名（最长匹配，确定性）。
        返回 (normalized_text, mappings[{raw, canonical, display, type}])。"""
        norm, mappings = text, []
        for term, info in sorted(self.term_index.items(), key=lambda kv: len(kv[0]), reverse=True):
            low = norm.lower()
            idx = low.find(term)
            if idx >= 0:
                norm = norm[:idx] + info["display"] + norm[idx + len(term):]
                mappings.append({"raw": term, "canonical": info["canonical"],
                                 "display": info["display"], "type": info["type"]})
        return norm, mappings
