"""本体同步检查（CI 用）：YAML ↔ Supabase 一致性验证。

用途（多人编辑闭环的 CI 防线）：
  1. 本地 YAML 与 Supabase 真源是否一致（export 后 diff）
  2. 若不一致：提示人工决定方向（YAML 评审通过 → import；DB 有更新 → export）

用法：
  python -m ontology_sync_check --yaml backend/ontology \
      --url <SUPABASE_URL> --key <KEY>
  退出码：0=一致；1=不一致（提示方向）；2=配置错误
  # 凭证不写死在本脚本：--url/--key 与 .env 环境变量是唯二来源。

注意：本脚本是**检查**不是自动同步——多人协作下自动覆盖有风险，
一致性维护走人工评审闭环（import/export 二选一）。

实现复用 core.ontology_store.SupabaseOntologyStore.load()——云端拉取、
软删除过滤、DB 行 → YAML 形状转换全部收敛在 core 一处，本脚本只做比较。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.ontology_store import (  # noqa: E402
    SUPABASE_ROW_KEYS, SupabaseOntologyStore, YamlOntologyStore)


def _canonical(rows: list[dict], keys: tuple) -> dict:
    """按主键对齐为 {key: 规范 dict}（键集一致，便于比较）。

    空值归一：None / '[]' / '' / [] / False 视为等价（导入时 NOT NULL
    列被补默认值，而 YAML 原文可能是 null——语义都是「无值」）。
    """
    def norm(v):
        if v is None or v == "[]" or v == "" or v == [] or v is False:
            return None
        return v

    key_field = keys[0]
    return {r.get(key_field): {k: norm(r.get(k)) for k in keys} for r in rows}


def check_sync(yaml_dir: Path, url: str, key: str, schema: str = "public") -> int:
    """比较本地 YAML 与 Supabase，返回 0=一致 / 1=不一致。"""
    local = YamlOntologyStore(yaml_dir).load()
    cloud = SupabaseOntologyStore(url, key, schema).load()

    diffs: list[str] = []
    fields = ["objects", "functions", "relations", "glossary"]
    for field in fields:
        keys = SUPABASE_ROW_KEYS["ontology_" + field]
        l = _canonical(getattr(local, field), keys)
        c = _canonical(getattr(cloud, field), keys)
        only_local = sorted(set(l) - set(c))
        only_cloud = sorted(set(c) - set(l))
        changed = sorted(k for k in set(l) & set(c) if l[k] != c[k])
        if only_local or only_cloud or changed:
            diffs.append(
                f"{field}: 仅本地 {len(only_local)} / 仅云端 {len(only_cloud)} / "
                f"变更 {len(changed)}"
                f"{' 例:' + str((only_local + only_cloud + changed)[:3]) if (only_local or only_cloud or changed) else ''}")

    # config 比较（load() 已把 JSON 值反序列化回 dict）
    if local.config != cloud.config:
        diffs.append(f"config: 本地 {local.config} ≠ 云端 {cloud.config}")

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
    ap.add_argument("--schema", default="public")
    a = ap.parse_args()
    url = a.url or os.environ.get("DATA_AGENT_SUPABASE_URL", "")
    key = a.key or os.environ.get("DATA_AGENT_SUPABASE_KEY", "")
    if not url or not key:
        print("❌ 需要 --url/--key 或环境变量 DATA_AGENT_SUPABASE_URL/KEY")
        return 2
    return check_sync(Path(a.yaml), url, key, schema=a.schema)


if __name__ == "__main__":
    sys.exit(main())
