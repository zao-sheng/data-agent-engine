"""表元数据入库工具：采集样例库表元数据 → Supabase ontology_tables。

用法：
  python -m ontology_tables_import --db backend/seed/sample.db \
      --url <SUPABASE_URL> --key <KEY>
  # 或从环境变量读 DATA_AGENT_SUPABASE_URL/KEY
  # 凭证不写死在本脚本：--url/--key 与 .env 环境变量是唯二来源。

流程：table_metadata.collect_table_metadata() → 键对齐 → SupabaseOntologyWriter
批量 upsert（on_conflict=table_name 幂等，重复执行更新不重复插入）。
实现复用 core.ontology_store.SupabaseOntologyWriter——HTTP 层、认证头、
冲突键全部收敛在 core 一处，本脚本只做采集与编排。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.ontology_loader import Ontology  # noqa: E402
from core.ontology_store import SupabaseOntologyWriter  # noqa: E402
from core.table_metadata import collect_table_metadata  # noqa: E402

# ontology_tables 表的列集（与 schema.sql 一致）
TABLES_KEYS = ("table_name", "layer", "domain", "subject", "description",
               "granularity", "partition_col", "owner_object",
               "fields", "lineage", "readiness")


def import_tables(url: str, key: str, db: Path, onto: Ontology,
                  schema: str = "public") -> int:
    metas = collect_table_metadata(db, onto)
    # 键对齐（PostgREST 批量 upsert 要求所有行键一致）
    rows = [{k: m.get(k) for k in TABLES_KEYS} for m in metas]
    if not rows:
        print("⏭  无表元数据可入库")
        return 1
    writer = SupabaseOntologyWriter(url, key, schema)
    n = writer.upsert_many("ontology_tables", rows)
    print(f"✅ 已入库 {n} 张表元数据 → ontology_tables")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="表元数据 → Supabase ontology_tables")
    ap.add_argument("--db", default=str(Path(__file__).resolve().parent / "seed" / "sample.db"))
    ap.add_argument("--ontology", default=str(Path(__file__).resolve().parent / "ontology"))
    ap.add_argument("--url", default=None)
    ap.add_argument("--key", default=None)
    ap.add_argument("--schema", default="public")
    a = ap.parse_args()

    url = a.url or os.environ.get("DATA_AGENT_SUPABASE_URL", "")
    key = a.key or os.environ.get("DATA_AGENT_SUPABASE_KEY", "")
    if not url or not key:
        print("❌ 需要 --url/--key 或环境变量 DATA_AGENT_SUPABASE_URL/KEY")
        return 1

    onto = Ontology(a.ontology)
    return import_tables(url, key, Path(a.db), onto, schema=a.schema)


if __name__ == "__main__":
    sys.exit(main())
