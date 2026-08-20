"""本体存储接口（Ontology Store）：把「本体数据从哪来」与「Ontology 索引构建」解耦。

设计：
  * Ontology（ontology_loader）只关心「原始数据 → 索引」——数据源可以是
    YAML 文件（发布基线快照）、SQLite 编译缓存（发布产物）、Supabase（多人编辑真源）
  * 换存储只实现一个新 Store 子类，Ontology 与全部调用方（translator /
    validator / metadata / intent / server 工具）零改动
  * 读取接口统一返回 `OntologyData`（原始 YAML 结构的 dict），索引构建复用

实现：
  * YamlOntologyStore —— 默认实现（读 backend/ontology/*.yaml）
  * SqliteOntologyStore —— 编译缓存（YAML → SQLite，发布时预构建）
  * SupabaseOntologyStore —— 多人编辑真源（PostgREST HTTP，urllib 零依赖）
"""
from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
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


# ── YamlStore（默认：发布基线快照源）─────────────────────────
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


# ── Supabase 存储（阶段 2：多人编辑真源）────────────────────────
# 走 PostgREST HTTP API（Supabase 标准 REST 端点，零额外重依赖）：
#   GET    {url}/rest/v1/{table}?select=*&is_deleted=eq.false
#   POST   {url}/rest/v1/{table}              （upsert，Prefer: resolution=merge-duplicates）
#   PATCH  {url}/rest/v1/{table}?name=eq.x    （按主键更新，带 revision 校验）
# 认证：apikey header（publishable / secret key 均可）。
SUPABASE_TABLES = {
    "objects": "ontology_objects",
    "functions": "ontology_functions",
    "relations": "ontology_relations",
    "glossary": "ontology_glossary",
    "config": "ontology_config",
}

# 每张表插入时的列集合（对齐 PostgREST 批量 upsert 的「所有行键一致」要求）
SUPABASE_ROW_KEYS = {
    "ontology_objects": ("name", "display_name", "description", "domain", "object_type",
                         "status", "data_owner", "tags", "security_level", "update_frequency",
                         "aliases", "required_filters", "properties", "source_tables",
                         "versions", "default_version"),
    "ontology_functions": ("name", "display_name", "description", "formula", "owner",
                           "domain", "category", "status", "data_owner", "unit", "tags",
                           "family", "variant_label", "default_of_family",
                           "required_filters", "supported_dimensions",
                           "supported_granularities", "do_not", "version"),
    "ontology_relations": ("source", "target", "type", "join_key", "cardinality", "description"),
    "ontology_glossary": ("term", "canonical", "type"),
    "ontology_config": ("key", "value"),
}


def normalize_rows(rows: list[dict], keys: tuple[str, ...]) -> list[dict]:
    """把行补全为统一键集（PostgREST 批量 upsert 要求所有行键一致）。

    YAML 对象/指标 dict 的行间键可能不一致（部分对象缺某些字段），
    批量插入前必须对齐；NOT NULL 列缺省补类型安全值：
      * JSONB 列补 []；BOOLEAN 列补 false；其余 TEXT 列补 ''
    避免触发 23502 not-null / 22P02 类型语法错误。
    """
    jsonb_cols = {"aliases", "required_filters", "properties", "source_tables",
                  "versions", "supported_dimensions", "supported_granularities",
                  "tags"}
    bool_cols = {"default_of_family"}
    out = []
    for row in rows:
        r = {}
        for k in keys:
            v = row.get(k)
            if v is None:
                if k in jsonb_cols:
                    r[k] = "[]"
                elif k in bool_cols:
                    r[k] = False
                else:
                    r[k] = ""
            else:
                r[k] = v
        out.append(r)
    return out


class _UrllibGetClient:
    """urllib 实现的轻量 GET client（兼容 requests 的 .get(path, params, headers)）。"""

    def __init__(self, url: str, key: str, schema: str = "public"):
        self.url = url.rstrip("/")
        self.key = key
        self.schema = schema

    def get(self, path: str, params: dict | None = None, headers: dict | None = None):
        qs = "&".join(f"{k}={v}" for k, v in (params or {}).items())
        full = f"{self.url}{path}" + (f"?{qs}" if qs else "")
        req = urllib.request.Request(full, headers={
            "apikey": self.key, "Authorization": f"Bearer {self.key}",
            "Accept-Profile": self.schema, **(headers or {})})
        # 网络抖动重试：timeout/URLError 重试 2 次（指数退避），HTTP 错误不重试
        import time as _time
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = resp.read().decode()
                break
            except urllib.error.HTTPError as e:
                body = e.read().decode()
                raise RuntimeError(f"GET {path} 失败: HTTP {e.code} {body[:200]}") from e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_exc = e
                if attempt < 2:
                    _time.sleep(0.3 * (2 ** attempt))
        else:
            raise RuntimeError(f"GET {path} 失败（重试 3 次仍超时/网络错误）: {last_exc}") from last_exc

        class _Resp:
            def raise_for_status(self):
                return None
            def json(self):
                return json.loads(body)
        return _Resp()


def _supabase_client() -> Any:
    """返回 urllib 实现的 GET client（零额外依赖，克隆即跑）。"""
    return _UrllibGetClient  # 由调用方传 url/key/schema 实例化


class SupabaseOntologyStore:
    """从 Supabase 读取本体（真源）。需 url/key（见 backend/.env）。"""

    def __init__(self, url: str, key: str, schema: str = "public"):
        self.url = url.rstrip("/")
        self.key = key
        self.schema = schema

    # ── 读接口 ──────────────────────────────────────────────
    def load(self) -> OntologyData:
        rows = {
            "objects": self._select("ontology_objects"),
            "functions": self._select("ontology_functions"),
            "relations": self._select("ontology_relations"),
            "glossary": self._select("ontology_glossary"),
            "config": self._select("ontology_config"),
        }
        return OntologyData(
            objects=[self._obj_to_yaml(r) for r in rows["objects"]],
            functions=[self._fn_to_yaml(r) for r in rows["functions"]],
            relations=[self._rel_to_yaml(r) for r in rows["relations"]],
            glossary=[self._gloss_to_yaml(r) for r in rows["glossary"]],
            config={r["key"]: json.loads(r["value"]) for r in rows["config"]},
        )

    def _select(self, table: str) -> list[dict]:
        params: dict[str, str] = {"select": "*"}
        # 只有带 is_deleted 列的业务表加软删除过滤；
        # meta/config 是纯键值表，无该列（加了会 42703 报错）
        if table not in ("ontology_meta", "ontology_config"):
            params["is_deleted"] = "eq.false"
        r = _supabase_client()(self.url, self.key, self.schema).get(
            f"/rest/v1/{table}",
            params=params,
            headers={"apikey": self.key, "Authorization": f"Bearer {self.key}",
                     "Accept-Profile": self.schema})
        r.raise_for_status()
        return r.json()

    # ── DB 行 → YAML 形状（与 YAML 文件结构一一对应）─────────
    # 用 row.get 容忍旧库/测试 mock 缺新列（云端旧表未跑迁移时仍可读）
    @staticmethod
    def _obj_to_yaml(row: dict) -> dict:
        defs = {"domain": "", "object_type": "fact", "status": "active",
                "data_owner": "", "tags": [], "security_level": "L2",
                "update_frequency": "T+1"}
        return {k: row.get(k, defs.get(k, "")) for k in
                ("name", "display_name", "description", "domain", "object_type",
                 "status", "data_owner", "tags", "security_level", "update_frequency",
                 "aliases", "required_filters", "properties", "source_tables",
                 "versions", "default_version")}

    @staticmethod
    def _fn_to_yaml(row: dict) -> dict:
        defs = {"domain": "", "category": "", "status": "active",
                "data_owner": "", "unit": "", "tags": []}
        return {k: row.get(k, defs.get(k, "")) for k in
                ("name", "display_name", "description", "formula", "owner",
                 "domain", "category", "status", "data_owner", "unit", "tags",
                 "family", "variant_label", "default_of_family",
                 "required_filters", "supported_dimensions",
                 "supported_granularities", "do_not", "version")}

    @staticmethod
    def _rel_to_yaml(row: dict) -> dict:
        return {k: row.get(k, "") for k in ("source", "target", "type", "join_key",
                                            "cardinality", "description")}

    @staticmethod
    def _gloss_to_yaml(row: dict) -> dict:
        return {k: row[k] for k in ("term", "canonical", "type")}


class SupabaseOntologyWriter:
    """本体写入（多人编辑/建模流程落地）。revision 乐观锁防冲突。

    用 urllib（标准库，零额外依赖）——requests 非必需，克隆即跑。
    """

    def __init__(self, url: str, key: str, schema: str = "public"):
        self.url = url.rstrip("/")
        self.key = key
        self.schema = schema

    def _headers(self) -> dict:
        return {"apikey": self.key, "Authorization": f"Bearer {self.key}",
                "Content-Profile": self.schema, "Accept-Profile": self.schema}

    # 每张表的 upsert 冲突键（on_conflict）——对应建表时的唯一约束
    CONFLICT_KEY = {
        "ontology_objects": "name",
        "ontology_functions": "name",
        "ontology_relations": "source,target,join_key",
        "ontology_glossary": "term",
        "ontology_config": "key",
        "ontology_tables": "table_name",
    }

    # 无 revision/updated_by 列的表（纯键值表，schema 无乐观锁列）
    NO_REVISION_TABLES = {"ontology_config", "ontology_meta"}

    def _upsert_row(self, table: str, row: dict, user: str = "") -> None:
        """upsert 单行（按唯一键合并），更新 revision+updated_by+updated_at。

        纯键值表（config/meta）schema 无 revision/updated_by 列，跳过这两列，
        避免 PostgREST 42703 undefined column。
        """
        payload = dict(row)
        if table not in self.NO_REVISION_TABLES:
            payload["revision"] = payload.get("revision", 1) + 1
            payload["updated_by"] = user
        url = f"{self.url}/rest/v1/{table}?on_conflict={self.CONFLICT_KEY[table]}"
        req = urllib.request.Request(
            url, data=json.dumps([payload]).encode(), method="POST",
            headers={**self._headers(),
                     "Content-Type": "application/json",
                     "Prefer": "resolution=merge-duplicates,return=minimal"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status not in (200, 201, 204):
                raise RuntimeError(f"upsert {table} 失败: HTTP {resp.status}")

    def upsert_many(self, table: str, rows: list[dict], user: str = "") -> int:
        """批量 upsert（幂等，按唯一键合并）。行须已对齐键集（normalize_rows）。

        供导入脚本（ontology_import / ontology_tables_import）复用——
        凭证只从调用方传入，脚本不硬编码 key。返回成功写入行数。
        """
        for row in rows:
            self._upsert_row(table, row, user)
        return len(rows)

    def soft_delete(self, table: str, conflict_key: str, key_value: str,
                    user: str = "") -> None:
        """软删除一行（is_deleted=true，评审可见可恢复）。

        多人编辑语义：删除不物理移除，标 is_deleted 让读路径过滤、
        管理端仍可审计。objects 按 name、tables 按 table_name 等。
        """
        url = (f"{self.url}/rest/v1/{table}"
               f"?{conflict_key}=eq.{key_value}&is_deleted=eq.false")
        req = urllib.request.Request(
            url, data=json.dumps({"is_deleted": True, "updated_by": user}).encode(),
            method="PATCH", headers={**self._headers(), "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status not in (200, 204):
                    raise RuntimeError(f"软删除 {table} 失败: HTTP {resp.status}")
        except urllib.error.HTTPError as e:
            if e.code == 404:  # 已不存在（可能已软删）→ 幂等成功
                return
            raise

    def upsert_object(self, obj: dict, user: str = "") -> None:
        self._upsert_row("ontology_objects", obj, user)

    def upsert_function(self, fn: dict, user: str = "") -> None:
        self._upsert_row("ontology_functions", fn, user)

    def upsert_relation(self, rel: dict, user: str = "") -> None:
        self._upsert_row("ontology_relations", rel, user)

    def upsert_glossary(self, term: dict, user: str = "") -> None:
        self._upsert_row("ontology_glossary", term, user)

    def upsert_config(self, key: str, value, user: str = "") -> None:
        self._upsert_row("ontology_config",
                         {"key": key, "value": json.dumps(value, ensure_ascii=False)}, user)

    def upsert_table(self, table: dict, user: str = "") -> None:
        """写入/更新 ontology_tables（表元数据，幂等按 table_name）。"""
        self._upsert_row("ontology_tables", table, user)

    def upsert_tables(self, tables: list[dict], user: str = "") -> None:
        """批量写入 ontology_tables（路径 D 落地后自动采集）。"""
        for t in tables:
            self.upsert_table(t, user)

    # ── 乐观锁冲突检测（可选：PATCH 带 revision 条件）─────────
    def update_object_if_revision(self, name: str, obj: dict, expected_revision: int,
                                  user: str = "") -> bool:
        """按 name + revision 更新；返回 False 表示冲突（他人已改）。"""
        payload = dict(obj)
        payload["revision"] = expected_revision + 1
        payload["updated_by"] = user
        url = (f"{self.url}/rest/v1/ontology_objects"
               f"?name=eq.{name}&revision=eq.{expected_revision}&is_deleted=eq.false")
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="PATCH",
                                     headers={**self._headers(), "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status == 200 and bool(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 204:  # 命中 0 行 → 冲突
                return False
            raise


class TableMetadataStore:
    """从 Supabase ontology_tables 查表元数据（metadata_search 的库数据源）。

    返回结构与 MetadataService.table_info 兼容（供上层直接消费）：
      {table, layer, layer_cn, description, granularity, partition_col,
       owner_object, fields[], lineage{source,logic}, readiness, row_estimate}
    """

    def __init__(self, url: str, key: str, schema: str = "public"):
        self.url = url.rstrip("/")
        self.key = key
        self.schema = schema

    def _select_one(self, table_name: str) -> dict | None:
        try:
            r = _supabase_client()(self.url, self.key, self.schema).get(
                f"/rest/v1/ontology_tables",
                params={"select": "*", "table_name": f"eq.{table_name}",
                        "is_deleted": "eq.false"},
                headers={"apikey": self.key, "Authorization": f"Bearer {self.key}",
                         "Accept-Profile": self.schema})
            r.raise_for_status()
            rows = r.json()
        except Exception:  # noqa: BLE001 —— 库不可用/表不存在时回落本地
            return None
        return rows[0] if rows else None

    def table_info(self, table: str) -> dict | None:
        row = self._select_one(table.lower())
        if row is None:
            return None
        return {
            "table": row.get("table_name", table),
            "layer": row.get("layer", ""),
            "layer_cn": {"DWD": "明细层", "DWS": "汇总层", "ADS": "应用层",
                         "DIM": "维度层"}.get(row.get("layer", ""), row.get("layer", "")),
            "description": row.get("description", ""),
            "granularity": row.get("granularity", ""),
            "partition_col": row.get("partition_col", ""),
            "owner_object": row.get("owner_object"),
            "fields": row.get("fields", []),
            "lineage": row.get("lineage", {}),
            "readiness": row.get("readiness", ""),
            "row_estimate": row.get("row_estimate"),
        }


# ── 工厂 ─────────────────────────────────────────────────────
def create_store(kind: str, base: Path | str | None = None,
                 db: Path | str | None = None,
                 url: str | None = None, key: str | None = None,
                 schema: str = "public") -> OntologyStore:
    """按 kind 创建 store：yaml（默认）/ sqlite / supabase。"""
    kind = (kind or "yaml").lower()
    if kind == "yaml":
        if base is None:
            raise ValueError("yaml store 需要 base（ontology 目录）")
        return YamlOntologyStore(base)
    if kind == "sqlite":
        if db is None:
            raise ValueError("sqlite store 需要 db（编译产物路径）")
        return SqliteOntologyStore(db)
    if kind == "supabase":
        if not url or not key:
            raise ValueError("supabase store 需要 url 与 key（DATA_AGENT_SUPABASE_URL/KEY）")
        return SupabaseOntologyStore(url, key, schema)
    raise ValueError(f"不支持的 ontology store: {kind}（支持 yaml / sqlite / supabase）")
