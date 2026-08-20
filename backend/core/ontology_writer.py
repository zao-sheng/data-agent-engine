"""本体写入器（多人编辑闭环）：按 store 类型分发到 YAML 文件或 Supabase。

设计：建模流程（modeling-workflow 阶段 2 本体注册）在用户确认后调用
`register_*` 系列写入。写入目标由 `DATA_AGENT_ONTOLOGY_STORE` 决定：
  * yaml    —— 写 backend/ontology/*.yaml（发布基线快照，单机）
  * supabase—— 写 Supabase 表（多人编辑真源）
  * sqlite  —— 只读编译产物，不支持写入（需先回灌 YAML 再编译）

与读接口（ontology_store.OntologyStore）对称：换存储只动这里，
Ontology 与调用方零改动。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .ontology_store import SupabaseOntologyWriter

# YAML 文件 → OntologyData 字段映射（与 YamlOntologyStore 对称）
YAML_FILE_BY_FIELD = {
    "objects": "objects.yaml",
    "functions": "functions.yaml",
    "relations": "relations.yaml",
    "glossary": "glossary.yaml",
}


class YamlOntologyWriter:
    """写 YAML 文件（默认：发布基线快照源）。"""

    def __init__(self, base: Path | str):
        self.base = Path(base)

    def _write_field(self, field: str, entries: list[dict]) -> None:
        fname = YAML_FILE_BY_FIELD[field]
        root_key = field
        payload = {root_key: entries}
        (self.base / fname).write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False,
                           default_flow_style=False),
            encoding="utf-8")

    def _upsert_entry(self, field: str, key_field: str, entry: dict) -> None:
        """upsert 单条到 YAML 数组（按 key_field 匹配，存在则替换）。"""
        fname = YAML_FILE_BY_FIELD[field]
        path = self.base / fname
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
        entries = raw.get(field, [])
        key = entry.get(key_field)
        replaced = False
        for i, e in enumerate(entries):
            if e.get(key_field) == key:
                entries[i] = entry
                replaced = True
                break
        if not replaced:
            entries.append(entry)
        self._write_field(field, entries)

    def upsert_object(self, obj: dict) -> None:
        self._upsert_entry("objects", "name", obj)

    def upsert_function(self, fn: dict) -> None:
        self._upsert_entry("functions", "name", fn)

    def upsert_relation(self, rel: dict) -> None:
        self._upsert_entry("relations", "source", rel)

    def upsert_glossary(self, term: dict) -> None:
        self._upsert_entry("glossary", "term", term)

    def upsert_config(self, key: str, value: Any) -> None:
        path = self.base / "config.yaml"
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
        cfg[key] = value
        path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")


class ReadOnlyWriter:
    """只读存储的写入占位（sqlite 编译产物）：所有写操作明确报错。

    用途：server 启动时统一 create_writer，sqlite 模式不抛异常（引擎能起），
    但任何 ontology_register 写入会得到清晰错误提示。
    """

    def __init__(self, kind: str = "sqlite"):
        self.kind = kind

    def _deny(self, *_a, **_k):
        raise ValueError(
            f"{self.kind} 是只读编译产物，不支持直接写入："
            "请写 backend/ontology/*.yaml 后重新 ontology_compile")

    upsert_object = _deny
    upsert_function = _deny
    upsert_relation = _deny
    upsert_glossary = _deny
    upsert_config = _deny


def create_writer(kind: str, base: Path | str | None = None,
                  url: str | None = None, key: str | None = None,
                  schema: str = "public") -> Any:
    """按 store 类型创建写入器。sqlite 只读（返回占位，写操作报错）。"""
    kind = (kind or "yaml").lower()
    if kind == "yaml":
        if base is None:
            raise ValueError("yaml writer 需要 base（ontology 目录）")
        return YamlOntologyWriter(base)
    if kind == "supabase":
        if not url or not key:
            raise ValueError("supabase writer 需要 url 与 key（DATA_AGENT_SUPABASE_URL/KEY）")
        return SupabaseOntologyWriter(url, key, schema)
    if kind == "sqlite":
        return ReadOnlyWriter("sqlite")
    raise ValueError(f"不支持的 ontology writer: {kind}（支持 yaml / supabase）")


def writer_supports(kind: str) -> bool:
    """该 store 类型是否支持写入（sqlite 只读）。"""
    return (kind or "yaml").lower() in ("yaml", "supabase")
