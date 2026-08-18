"""Ontology 加载器：读 YAML，构建对象/函数/关系/属性归属索引。

翻译引擎（JOIN 链）与 MQL 校验器（属性存在性）都以这里的索引为准。
"""
from __future__ import annotations

from collections import deque
from pathlib import Path

import yaml

# 维度对象 → SQL 别名（事实表固定为 F）
DIM_ALIAS = {"Product": "P", "Store": "S", "Region": "R", "ActiveUser": "U"}


class Ontology:
    def __init__(self, base: Path | str):
        base = Path(base)
        self.objects: dict[str, dict] = {}
        self.functions: dict[str, dict] = {}
        self.relations: list[dict] = []
        self.property_owner: dict[str, str] = {}   # 业务属性 → 归属对象
        self.edges: dict[str, list[tuple[str, str]]] = {}  # source → [(target, join_key)]

        raw_objects = yaml.safe_load((base / "objects.yaml").read_text())["objects"]
        raw_functions = yaml.safe_load((base / "functions.yaml").read_text())["functions"]
        self.relations = yaml.safe_load((base / "relations.yaml").read_text())["relations"]

        for o in raw_objects:
            self.objects[o["name"]] = o
            for p in o.get("properties", []):
                self.property_owner[p["name"]] = o["name"]
        for f in raw_functions:
            self.functions[f["name"]] = f
        for r in self.relations:
            self.edges.setdefault(r["source"], []).append((r["target"], r["join_key"]))

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
        return DIM_ALIAS.get(obj_name, obj_name[0].upper())
