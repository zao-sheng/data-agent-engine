"""本体导入工具：YAML（Git 评审源）→ Supabase（多人编辑真源）。

用法：
  python -m ontology_import --yaml backend/ontology \
      --url <SUPABASE_URL> --key <SERVICE_OR_SECRET_KEY>
  # 或从环境变量读：DATA_AGENT_SUPABASE_URL / DATA_AGENT_SUPABASE_KEY

流程：YamlOntologyStore.load() → 键对齐（normalize_rows）→ 批量 upsert。
幂等：PostgREST upsert（Prefer: resolution=merge-duplicates）按唯一键合并，
重复执行安全；relations/glossary 按唯一约束合并，objects/functions 按 name。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.ontology_store import (  # noqa: E402
    SUPABASE_ROW_KEYS, YamlOntologyStore, normalize_rows)

IMPORT_ORDER = [
    ("ontology_objects", "objects"),
    ("ontology_functions", "functions"),
    ("ontology_relations", "relations"),
    ("ontology_glossary", "glossary"),
]


def _req(url: str, key: str, method: str, path: str,
         payload: list[dict] | None = None, params: dict | None = None) -> tuple[int, str]:
    full = url.rstrip("/") + path
    if params:
        full += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    data = json.dumps(payload).encode() if payload else None
    r = urllib.request.Request(full, data=data, method=method, headers={
        "apikey": key, "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
        "Content-Profile": "public"})
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def import_to_supabase(url: str, key: str, yaml_dir: Path) -> int:
    """YAML → Supabase 批量导入。返回成功导入的表数。"""
    store = YamlOntologyStore(yaml_dir)
    data = store.load()
    # 每张表的 upsert 冲突键（on_conflict）——对应建表时的唯一约束
    CONFLICT_KEY = {
        "ontology_objects": "name",
        "ontology_functions": "name",
        "ontology_relations": "source,target,join_key",
        "ontology_glossary": "term",
        "ontology_config": "key",
    }
    ok = 0
    for table, field in IMPORT_ORDER:
        rows = normalize_rows(getattr(data, field), SUPABASE_ROW_KEYS[table])
        if not rows:
            print(f"  ⏭  {table}: 无数据，跳过")
            continue
        s, body = _req(url, key, "POST", f"/rest/v1/{table}", rows,
                       params={"on_conflict": CONFLICT_KEY[table]})
        if s in (200, 201):
            print(f"  ✓ {table}: {len(rows)} 行")
            ok += 1
        else:
            print(f"  ✗ {table}: HTTP {s} {body[:200]}")
    # config：单条键值
    cfg_rows = [{"key": k, "value": json.dumps(v, ensure_ascii=False)}
                for k, v in data.config.items()]
    if cfg_rows:
        s, body = _req(url, key, "POST", "/rest/v1/ontology_config", cfg_rows,
                       params={"on_conflict": "key"})
        if s in (200, 201):
            print(f"  ✓ ontology_config: {len(cfg_rows)} 条")
            ok += 1
        else:
            print(f"  ✗ ontology_config: HTTP {s} {body[:200]}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="YAML 本体 → Supabase 导入")
    ap.add_argument("--yaml", default="ontology", help="ontology YAML 目录")
    ap.add_argument("--url", default=None, help="Supabase URL（默认读环境变量）")
    ap.add_argument("--key", default=None, help="Supabase key（默认读环境变量）")
    a = ap.parse_args()

    url = a.url or os.environ.get("DATA_AGENT_SUPABASE_URL", "")
    key = a.key or os.environ.get("DATA_AGENT_SUPABASE_KEY", "")
    if not url or not key:
        print("❌ 需要 --url/--key 或环境变量 DATA_AGENT_SUPABASE_URL/KEY")
        return 1

    ok = import_to_supabase(url, key, Path(a.yaml))
    print(f"\n{'✅' if ok == len(IMPORT_ORDER) + 1 else '⚠️'} 导入完成: {ok}/{len(IMPORT_ORDER) + 1} 张表成功")
    return 0 if ok == len(IMPORT_ORDER) + 1 else 1


if __name__ == "__main__":
    sys.exit(main())
