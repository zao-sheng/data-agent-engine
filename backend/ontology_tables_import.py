"""表元数据入库工具：采集样例库表元数据 → Supabase ontology_tables。

用法：
  python -m ontology_tables_import --db backend/seed/sample.db \
      --url <SUPABASE_URL> --key <KEY>
  # 或从环境变量读 DATA_AGENT_SUPABASE_URL/KEY

流程：table_metadata.collect_table_metadata() → 键对齐 → 批量 upsert
      （on_conflict=table_name 幂等，重复执行更新不重复插入）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.ontology_loader import Ontology  # noqa: E402
from core.table_metadata import collect_table_metadata  # noqa: E402

# ontology_tables 表的列集（与 schema.sql 一致）
TABLES_KEYS = ("table_name", "layer", "domain", "subject", "description",
               "granularity", "partition_col", "owner_object",
               "fields", "lineage", "readiness")


def import_tables(url: str, key: str, db: Path, onto: Ontology) -> int:
    metas = collect_table_metadata(db, onto)
    # 键对齐（PostgREST 批量 upsert 要求所有行键一致）
    rows = [{k: m.get(k) for k in TABLES_KEYS} for m in metas]
    payload = json.dumps(rows).encode()
    r = urllib.request.Request(
        f"{url.rstrip('/')}/rest/v1/ontology_tables?on_conflict=table_name",
        data=payload, method="POST",
        headers={"apikey": key, "Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "Prefer": "resolution=merge-duplicates,return=minimal",
                 "Content-Profile": "public"})
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        print(f"✗ 入库失败: HTTP {e.code} {e.read().decode()[:200]}")
        return 1
    if status in (200, 201):
        print(f"✅ 已入库 {len(rows)} 张表元数据 → ontology_tables")
        return 0
    print(f"✗ 未知状态 HTTP {status}")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="表元数据 → Supabase ontology_tables")
    ap.add_argument("--db", default=str(Path(__file__).resolve().parent / "seed" / "sample.db"))
    ap.add_argument("--ontology", default=str(Path(__file__).resolve().parent / "ontology"))
    ap.add_argument("--url", default=None)
    ap.add_argument("--key", default=None)
    a = ap.parse_args()

    url = a.url or os.environ.get("DATA_AGENT_SUPABASE_URL", "")
    key = a.key or os.environ.get("DATA_AGENT_SUPABASE_KEY", "")
    if not url or not key:
        print("❌ 需要 --url/--key 或环境变量 DATA_AGENT_SUPABASE_URL/KEY")
        return 1

    onto = Ontology(a.ontology)
    return import_tables(url, key, Path(a.db), onto)


if __name__ == "__main__":
    sys.exit(main())
