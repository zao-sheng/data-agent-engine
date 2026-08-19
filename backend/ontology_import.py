"""本体导入工具：YAML（发布基线快照）→ Supabase（多人编辑真源）。

用法：
  python -m ontology_import --yaml backend/ontology \
      --url <SUPABASE_URL> --key <SERVICE_OR_SECRET_KEY>
  # 或从环境变量读：DATA_AGENT_SUPABASE_URL / DATA_AGENT_SUPABASE_KEY
  # 凭证不写死在本脚本：--url/--key 与 .env 环境变量是唯二来源。

流程：YamlOntologyStore.load() → 键对齐（normalize_rows）→ SupabaseOntologyWriter
批量 upsert（幂等：按唯一键合并，重复执行安全）。
实现复用 core.ontology_store.SupabaseOntologyWriter——HTTP 层、认证头、冲突键
全部收敛在 core 一处，本脚本只编排数据。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.ontology_store import (  # noqa: E402
    SUPABASE_ROW_KEYS, SupabaseOntologyWriter, YamlOntologyStore, normalize_rows)

# 导入顺序：业务数据表 + config
IMPORT_ORDER = [
    ("ontology_objects", "objects"),
    ("ontology_functions", "functions"),
    ("ontology_relations", "relations"),
    ("ontology_glossary", "glossary"),
]


def import_to_supabase(url: str, key: str, yaml_dir: Path,
                       schema: str = "public") -> int:
    """YAML → Supabase 批量导入。返回成功导入的表数。"""
    store = YamlOntologyStore(yaml_dir)
    data = store.load()
    writer = SupabaseOntologyWriter(url, key, schema)
    ok = 0
    for table, field in IMPORT_ORDER:
        rows = normalize_rows(getattr(data, field), SUPABASE_ROW_KEYS[table])
        if not rows:
            print(f"  ⏭  {table}: 无数据，跳过")
            continue
        n = writer.upsert_many(table, rows)
        print(f"  ✓ {table}: {n} 行")
        ok += 1
    # config：单条键值（值 JSON 序列化，与 writer.upsert_config 一致）
    cfg_rows = [{"key": k, "value": json.dumps(v, ensure_ascii=False)}
                for k, v in data.config.items()]
    if cfg_rows:
        n = writer.upsert_many("ontology_config", cfg_rows)
        print(f"  ✓ ontology_config: {n} 条")
        ok += 1
    else:
        print("  ⏭  ontology_config: 无数据，跳过")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="YAML 本体 → Supabase 导入")
    ap.add_argument("--yaml", default="ontology", help="ontology YAML 目录")
    ap.add_argument("--url", default=None, help="Supabase URL（默认读环境变量）")
    ap.add_argument("--key", default=None, help="Supabase key（默认读环境变量）")
    ap.add_argument("--schema", default="public")
    a = ap.parse_args()

    url = a.url or os.environ.get("DATA_AGENT_SUPABASE_URL", "")
    key = a.key or os.environ.get("DATA_AGENT_SUPABASE_KEY", "")
    if not url or not key:
        print("❌ 需要 --url/--key 或环境变量 DATA_AGENT_SUPABASE_URL/KEY")
        return 1

    ok = import_to_supabase(url, key, Path(a.yaml), schema=a.schema)
    total = len(IMPORT_ORDER) + 1
    print(f"\n{'✅' if ok == total else '⚠️'} 导入完成: {ok}/{total} 张表成功")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
