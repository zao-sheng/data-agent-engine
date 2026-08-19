"""本体编译工具：YAML（发布基线快照）→ SQLite（发布产物）。

用法：
  python -m ontology_compile --yaml backend/ontology --out backend/ontology.db
  # 可选：--commit 记录来源 commit（CI 里用 git rev-parse --short HEAD）

流程：YamlOntologyStore.load() → compile_to_sqlite() → SqliteOntologyStore 可读回。
幂等：先清空再写，重复执行安全。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.ontology_store import YamlOntologyStore, compile_to_sqlite  # noqa: E402


def _current_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=Path(__file__).resolve().parent.parent.parent,
            timeout=5).stdout.strip()
    except Exception:
        return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="YAML 本体 → SQLite 编译产物")
    ap.add_argument("--yaml", default="ontology", help="ontology YAML 目录")
    ap.add_argument("--out", default="ontology.db", help="输出 SQLite 路径")
    ap.add_argument("--commit", action="store_true", help="记录当前 git commit 到产物 meta")
    a = ap.parse_args()

    store = YamlOntologyStore(a.yaml)
    data = store.load()
    commit = _current_commit() if a.commit else ""
    compile_to_sqlite(data, a.out, source_commit=commit)
    print(f"✅ 本体已编译: {a.out}")
    print(f"   objects={len(data.objects)} functions={len(data.functions)} "
          f"relations={len(data.relations)} glossary={len(data.glossary)}")
    if commit:
        print(f"   来源 commit: {commit}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
