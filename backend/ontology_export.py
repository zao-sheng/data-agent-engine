"""本体导出工具：Supabase（真源）→ YAML（Git 评审副本）。

用法：
  python -m ontology_export --url <SUPABASE_URL> --key <SERVICE_KEY> \
      --out backend/ontology   # 导出 5 个 YAML，覆盖 ontology 目录
  # 或从环境变量读：DATA_AGENT_SUPABASE_URL / DATA_AGENT_SUPABASE_KEY

闭环：管理端直写 Supabase → 本工具导出 YAML → 提交 PR 评审 →
      评审通过合并（可选：ontology_compile 编译 SQLite 作为发布基线）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.ontology_store import SupabaseOntologyStore  # noqa: E402

# YAML 文件 → OntologyData 字段映射（与 YamlOntologyStore 对称）
EXPORT_MAP = [
    ("objects.yaml", "objects"),
    ("functions.yaml", "functions"),
    ("relations.yaml", "relations"),
    ("glossary.yaml", "glossary"),
]


def export_to_yaml(data, out_dir: Path) -> None:
    """把 OntologyData 写回 YAML（objects/functions/relations/glossary/config）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    for fname, field in EXPORT_MAP:
        rows = getattr(data, field)
        # glossary/objects 等 YAML 根键约定（与源文件一致）
        root_key = {"objects": "objects", "functions": "functions",
                    "relations": "relations", "glossary": "glossary"}[field]
        payload = {root_key: rows}
        (out_dir / fname).write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False,
                           default_flow_style=False),
            encoding="utf-8")
    # config.yaml：平铺 key/value（与源结构一致）
    (out_dir / "config.yaml").write_text(
        yaml.safe_dump(dict(data.config), allow_unicode=True, sort_keys=False),
        encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Supabase 本体 → YAML 导出")
    ap.add_argument("--url", default=None, help="Supabase URL（默认读环境变量）")
    ap.add_argument("--key", default=None, help="Supabase key（默认读环境变量）")
    ap.add_argument("--out", default="ontology", help="输出 YAML 目录")
    ap.add_argument("--schema", default="public")
    a = ap.parse_args()

    import os
    url = a.url or os.environ.get("DATA_AGENT_SUPABASE_URL", "")
    key = a.key or os.environ.get("DATA_AGENT_SUPABASE_KEY", "")
    if not url or not key:
        print("❌ 需要 --url/--key 或环境变量 DATA_AGENT_SUPABASE_URL/KEY")
        return 1

    store = SupabaseOntologyStore(url, key, schema=a.schema)
    data = store.load()
    export_to_yaml(data, Path(a.out))
    print(f"✅ 已导出 Supabase 本体 → {a.out}")
    print(f"   objects={len(data.objects)} functions={len(data.functions)} "
          f"relations={len(data.relations)} glossary={len(data.glossary)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
