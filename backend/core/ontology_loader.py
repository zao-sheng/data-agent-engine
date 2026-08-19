"""Ontology：从原始本体数据构建索引（对象/函数/关系/属性归属/黑话/搜索）。

数据源由 OntologyStore 提供（YAML 默认 / SQLite 编译缓存 / Supabase 预留），
本类只负责「原始数据 → 索引」，与存储解耦（见 core/ontology_store.py）。

翻译引擎（JOIN 链）与 MQL 校验器（属性存在性）都以这里的索引为准。
"""
from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any

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

        self.time_dim: str = cfg.get("time_dimension", "order_date")
        self.partition_col: str = cfg.get("partition_column", "dt")

        for o in raw_objects:
            self.objects[o["name"]] = o
            for p in o.get("properties", []):
                self.property_owner[p["name"]] = o["name"]
        for f in raw_functions:
            self.functions[f["name"]] = f
        for r in self.relations:
            self.edges.setdefault(r["source"], []).append((r["target"], r["join_key"]))

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
            if o.get("source_tables", [{}])[0].get("layer") != "DIM":
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
            line = (f"[对象] {name}（{o.get('display_name')}）：{o.get('description','')}"
                    f"；属性: {props}")
            for key in {name, o.get("display_name", ""), *o.get("aliases", [])}:
                if key:
                    self._index_add(str(key).lower(), line)
        for f in raw_functions:
            line = (f"[指标] {f['name']}（{f.get('display_name')}）：{f.get('description','')}"
                    f"；公式: {f['formula']}；版本: {f.get('version')}")
            for key in {f["name"], f.get("display_name", "")}:
                if key:
                    self._index_add(str(key).lower(), line)
        for p in self.property_owner:
            self._index_add(p.lower(), f"[属性] {p}（归属 {self.owner_of(p)}）")

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
