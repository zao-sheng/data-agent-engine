"""本体同步检查（CI 用）：YAML ↔ Supabase 一致性验证。

用途（多人编辑闭环的 CI 防线）：
  1. 本地 YAML 与 Supabase 真源是否一致（export 后 diff）
  2. 若不一致：提示人工决定方向（YAML 评审通过 → import；DB 有更新 → export）

用法：
  python -m ontology_sync_check --yaml backend/ontology \
      --url <SUPABASE_URL> --key <KEY>
  退出码：0=一致；1=不一致（提示方向）；2=配置错误

注意：本脚本是**检查**不是自动同步——多人协作下自动覆盖有风险，
一致性维护走人工评审闭环（import/export 二选一）。
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


def _fetch_supabase(url: str, key: str) -> dict:
    """拉取 Supabase 全量数据（返回 YAML 形状）。"""
    def get(table):
        u = f"{url.rstrip('/')}/rest/v1/{table}?select=*"
        # config/meta 无 is_deleted 列，不加软删除过滤（避免 42703）
        if table not in ("ontology_config", "ontology_meta"):
            u += "&is_deleted=eq.false"
        r = urllib.request.Request(u, headers={"apikey": key,
                                               "Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(r, timeout=20) as resp:
            return json.loads(resp.read().decode())

    OBJ_K = ("name", "display_name", "description", "aliases", "required_filters",
             "properties", "source_tables", "versions", "default_version")
    FN_K = ("name", "display_name", "description", "formula", "owner", "family",
            "variant_label", "default_of_family", "required_filters",
            "supported_dimensions", "supported_granularities", "do_not", "version")
    REL_K = ("source", "target", "type", "join_key", "cardinality")
    GL_K = ("term", "canonical", "type")

    cfg = get("ontology_config")
    return {
        "objects": [{k: o.get(k) for k in OBJ_K} for o in get("ontology_objects")],
        "functions": [{k: f.get(k) for k in FN_K} for f in get("ontology_functions")],
        "relations": [{k: r.get(k) for k in REL_K} for r in get("ontology_relations")],
        "glossary": [{k: g.get(k) for k in GL_K} for g in get("ontology_glossary")],
        "config": {r["key"]: json.loads(r["value"]) for r in cfg},
    }


def _canonical(rows: list[dict], keys: tuple) -> dict:
    """按主键对齐为 {key: 规范 dict}（键集一致，便于比较）。

    空值归一：None / '[]' / '' / [] 视为等价（导入时 NOT NULL 列被补默认值，
    而 YAML 原文可能是 null——语义都是「无值」）。
    """
    def norm(v):
        # 空值归一：None / '[]' / '' / [] / False 视为等价
        # （导入时 NOT NULL/BOOLEAN 列被补默认值，而 YAML 原文可能是
        #   null/缺失——语义都是「无值」或「非默认」）
        if v is None or v == "[]" or v == "" or v == [] or v is False:
            return None
        return v

    key_field = keys[0]
    return {r.get(key_field): {k: norm(r.get(k)) for k in keys} for r in rows}


def check_sync(yaml_dir: Path, url: str, key: str) -> int:
    """比较本地 YAML 与 Supabase，返回 0=一致 / 1=不一致。"""
    local = YamlOntologyStore(yaml_dir).load()
    cloud = _fetch_supabase(url, key)

    diffs: list[str] = []
    fields = ["objects", "functions", "relations", "glossary"]
    for field in fields:
        keys = SUPABASE_ROW_KEYS["ontology_" + field]
        l = _canonical(getattr(local, field), keys)
        c = _canonical(cloud[field], keys)
        only_local = sorted(set(l) - set(c))
        only_cloud = sorted(set(c) - set(l))
        changed = sorted(k for k in set(l) & set(c) if l[k] != c[k])
        if only_local or only_cloud or changed:
            diffs.append(
                f"{field}: 仅本地 {len(only_local)} / 仅云端 {len(only_cloud)} / "
                f"变更 {len(changed)}"
                f"{' 例:' + str((only_local + only_cloud + changed)[:3]) if (only_local or only_cloud or changed) else ''}")

    # config 比较
    if local.config != cloud["config"]:
        diffs.append(f"config: 本地 {local.config} ≠ 云端 {cloud['config']}")

    if not diffs:
        print("✅ YAML ↔ Supabase 本体一致")
        return 0
    print("⚠️ YAML ↔ Supabase 本体不一致：")
    for d in diffs:
        print(f"  - {d}")
    print("  方向选择：本地有新变更且评审通过 → ontology_import 推送云端；")
    print("            云端有他人更新 → ontology_export 拉回 YAML 评审。")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="YAML ↔ Supabase 本体一致性检查")
    ap.add_argument("--yaml", default="ontology")
    ap.add_argument("--url", default=None)
    ap.add_argument("--key", default=None)
    a = ap.parse_args()
    url = a.url or os.environ.get("DATA_AGENT_SUPABASE_URL", "")
    key = a.key or os.environ.get("DATA_AGENT_SUPABASE_KEY", "")
    if not url or not key:
        print("❌ 需要 --url/--key 或环境变量 DATA_AGENT_SUPABASE_URL/KEY")
        return 2
    return check_sync(Path(a.yaml), url, key)


if __name__ == "__main__":
    sys.exit(main())
