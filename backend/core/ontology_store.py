"""本体存储接口（Ontology Store）：把「本体数据从哪来」与「Ontology 索引构建」解耦。

设计动机（为 Supabase 铺路）：
  * Ontology（ontology_loader）只关心「原始数据 → 索引」——数据源可以是
    YAML 文件（Git 评审源）、SQLite 编译缓存（发布产物）、Supabase（多人编辑真源）
  * 换存储只实现一个新 Store 子类，Ontology 与全部调用方（translator /
    validator / metadata / intent / server 工具）零改动
  * 读取接口统一返回 `OntologyData`（原始 YAML 结构的 dict），索引构建复用

实现：
  * YamlOntologyStore —— 默认实现（读 backend/ontology/*.yaml）
  * SqliteOntologyStore —— 编译缓存（YAML → SQLite，发布时预构建）
  * SupabaseOntologyStore —— 预留（接入 Supabase 后实现同一接口，见文档）
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml


@dataclass
class OntologyData:
    """本体原始数据（与 YAML 文件结构一一对应）。"""
    objects: list[dict] = field(default_factory=list)
    functions: list[dict] = field(default_factory=list)
    relations: list[dict] = field(default_factory=list)
    glossary: list[dict] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)


class OntologyStore(Protocol):
    """本体存储接口：load() 返回原始本体数据。"""
    def load(self) -> OntologyData: ...


# ── YamlStore（默认：Git 评审源）─────────────────────────────
class YamlOntologyStore:
    """从 backend/ontology/*.yaml 读取（默认实现，clone 即跑、零依赖）。"""

    FILES = ["objects.yaml", "functions.yaml", "relations.yaml", "glossary.yaml", "config.yaml"]

    def __init__(self, base: Path | str):
        self.base = Path(base)

    def load(self) -> OntologyData:
        def _read(name: str) -> Any:
            p = self.base / name
            return yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else None

        objects = _read("objects.yaml") or {}
        functions = _read("functions.yaml") or {}
        relations = _read("relations.yaml") or {}
        glossary = _read("glossary.yaml") or {}
        cfg = _read("config.yaml") or {}
        return OntologyData(
            objects=objects.get("objects", []),
            functions=functions.get("functions", []),
            relations=relations.get("relations", []),
            glossary=glossary.get("glossary", []),
            config=cfg,
        )


# ── SqliteStore（编译缓存：发布时 YAML → SQLite）──────────────
# 表结构（与 YAML 结构一一对应，data 列存 JSON 序列化的原始条目）：
#   ontology_meta(key, value)              —— schema_version / compiled_at / source_commit
#   objects(name PRIMARY KEY, data)        —— 每个业务对象一行
#   functions(name PRIMARY KEY, data)      —— 每个指标一行
#   relations(id, data)                    —— 关系边
#   glossary(id, data)                     —— 黑话条目
#   config(key PRIMARY KEY, value)         —— 全局配置
SCHEMA_VERSION = "1"
DDL = """
CREATE TABLE IF NOT EXISTS ontology_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS objects (
  name TEXT PRIMARY KEY,
  data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS functions (
  name TEXT PRIMARY KEY,
  data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS relations (
  id   INTEGER PRIMARY KEY AUTOINCREMENT,
  data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS glossary (
  id   INTEGER PRIMARY KEY AUTOINCREMENT,
  data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS config (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def compile_to_sqlite(data: OntologyData, out: Path | str,
                      source_commit: str = "") -> None:
    """把 OntologyData 编译写入 SQLite（幂等：先清空再写）。"""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(out)
    try:
        conn.executescript(DDL)
        conn.execute("DELETE FROM ontology_meta")
        conn.execute("DELETE FROM objects")
        conn.execute("DELETE FROM functions")
        conn.execute("DELETE FROM relations")
        conn.execute("DELETE FROM glossary")
        conn.execute("DELETE FROM config")
        conn.executemany("INSERT INTO objects(name, data) VALUES (?,?)",
                         [(o["name"], json.dumps(o, ensure_ascii=False))
                          for o in data.objects])
        conn.executemany("INSERT INTO functions(name, data) VALUES (?,?)",
                         [(f["name"], json.dumps(f, ensure_ascii=False))
                          for f in data.functions])
        conn.executemany("INSERT INTO relations(data) VALUES (?)",
                         [(json.dumps(r, ensure_ascii=False),) for r in data.relations])
        conn.executemany("INSERT INTO glossary(data) VALUES (?)",
                         [(json.dumps(g, ensure_ascii=False),) for g in data.glossary])
        conn.executemany("INSERT INTO config(key, value) VALUES (?,?)",
                         [(k, json.dumps(v, ensure_ascii=False))
                          for k, v in data.config.items()])
        import time
        conn.execute("INSERT OR REPLACE INTO ontology_meta(key, value) VALUES (?,?)",
                     ("schema_version", SCHEMA_VERSION))
        conn.execute("INSERT OR REPLACE INTO ontology_meta(key, value) VALUES (?,?)",
                     ("compiled_at", time.strftime("%Y-%m-%dT%H:%M:%S%z")))
        if source_commit:
            conn.execute("INSERT OR REPLACE INTO ontology_meta(key, value) VALUES (?,?)",
                         ("source_commit", source_commit))
        conn.commit()
    finally:
        conn.close()


class SqliteOntologyStore:
    """从编译产物 SQLite 读取（发布后只读 DB；源仍是 YAML）。"""

    def __init__(self, db: Path | str):
        self.db = Path(db)

    def load(self) -> OntologyData:
        if not self.db.exists():
            raise FileNotFoundError(
                f"本体编译产物不存在: {self.db}。请先运行 ontology_compile "
                f"（或改用默认 YamlOntologyStore）")
        conn = sqlite3.connect(self.db)
        try:
            rows = conn.execute("SELECT data FROM objects ORDER BY name").fetchall()
            objects = [json.loads(r[0]) for r in rows]
            rows = conn.execute("SELECT data FROM functions ORDER BY name").fetchall()
            functions = [json.loads(r[0]) for r in rows]
            rows = conn.execute("SELECT data FROM relations ORDER BY id").fetchall()
            relations = [json.loads(r[0]) for r in rows]
            rows = conn.execute("SELECT data FROM glossary ORDER BY id").fetchall()
            glossary = [json.loads(r[0]) for r in rows]
            rows = conn.execute("SELECT key, value FROM config").fetchall()
            config = {k: json.loads(v) for k, v in rows}
        finally:
            conn.close()
        return OntologyData(objects=objects, functions=functions,
                            relations=relations, glossary=glossary, config=config)


# ── 工厂 ─────────────────────────────────────────────────────
def create_store(kind: str, base: Path | str | None = None,
                 db: Path | str | None = None) -> OntologyStore:
    """按 kind 创建 store：yaml（默认）/ sqlite / supabase（预留）。"""
    kind = (kind or "yaml").lower()
    if kind == "yaml":
        if base is None:
            raise ValueError("yaml store 需要 base（ontology 目录）")
        return YamlOntologyStore(base)
    if kind == "sqlite":
        if db is None:
            raise ValueError("sqlite store 需要 db（编译产物路径）")
        return SqliteOntologyStore(db)
    raise ValueError(f"不支持的 ontology store: {kind}（支持 yaml / sqlite / supabase）")
