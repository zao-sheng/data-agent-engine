"""本体导出工具：Supabase（真源）→ YAML（发布基线快照）。

定位：本体元数据维护发生在可视化系统（Web 界面 + 内置审批流），
Git 中的 YAML 只是「发布基线的审计快照」——用于追溯/回滚/对外交付，
不承担评审职责（评审在可视化系统的审批流里完成）。

用法：
  # 仅导出 YAML（发布基线快照）
  python -m ontology_export --url <URL> --key <KEY> --out backend/ontology

  # 导出 + 建分支 + 提交 + 推送（可选：提交基线快照留痕，Git 仓库内）
  python -m ontology_export --url <URL> --key <KEY> \
      --out backend/ontology --branch ontology/snapshot-20260101
"""
from __future__ import annotations

import argparse
import subprocess
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


def _run(cmd: list[str], repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=60)


def git_diff_stat(repo: Path) -> str:
    """导出后变更量（供展示）。"""
    r = _run(["git", "diff", "--stat"], repo)
    return r.stdout.strip()


def create_pr_branch(repo: Path, branch: str) -> tuple[bool, str]:
    """建分支 + 提交 + 推送。返回 (ok, message)。"""
    # 检查 git 仓库
    if not (repo / ".git").exists():
        return False, "不是 git 仓库（缺少 .git），跳过 PR"
    # 建分支（幂等：已存在则切过去）
    r = _run(["git", "checkout", "-B", branch], repo)
    if r.returncode != 0:
        return False, f"建分支失败: {r.stderr.strip()}"
    # 提交（仅 ontology 目录）
    r = _run(["git", "add", "backend/ontology/"], repo)
    if r.returncode != 0:
        return False, f"git add 失败: {r.stderr.strip()}"
    r = _run(["git", "commit", "-m", f"本体更新（来自 Supabase 导出）"], repo)
    if r.returncode != 0 and "nothing to commit" not in r.stdout:
        return False, f"git commit 失败: {r.stderr.strip() or r.stdout.strip()}"
    # 推送
    r = _run(["git", "push", "-u", "origin", branch], repo)
    if r.returncode != 0:
        return False, f"git push 失败: {r.stderr.strip()}"
    return True, f"已推送分支 {branch}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Supabase 本体 → YAML 导出（发布基线快照）")
    ap.add_argument("--url", default=None, help="Supabase URL（默认读环境变量）")
    ap.add_argument("--key", default=None, help="Supabase key（默认读环境变量）")
    ap.add_argument("--out", default="ontology", help="输出 YAML 目录")
    ap.add_argument("--schema", default="public")
    ap.add_argument("--branch", default=None,
                    help="导出一并建分支提交推送（基线快照留痕，可选）")
    a = ap.parse_args()

    import os
    url = a.url or os.environ.get("DATA_AGENT_SUPABASE_URL", "")
    key = a.key or os.environ.get("DATA_AGENT_SUPABASE_KEY", "")
    if not url or not key:
        print("❌ 需要 --url/--key 或环境变量 DATA_AGENT_SUPABASE_URL/KEY")
        return 1

    # 1. 导出
    store = SupabaseOntologyStore(url, key, schema=a.schema)
    data = store.load()
    export_to_yaml(data, Path(a.out))
    print(f"✅ 已导出 Supabase 本体 → {a.out}")
    print(f"   objects={len(data.objects)} functions={len(data.functions)} "
          f"relations={len(data.relations)} glossary={len(data.glossary)}")

    # 2. 可选：建分支提交推送（基线快照留痕）
    if a.branch:
        cur = Path(a.out).resolve()
        while not (cur / ".git").exists() and cur.parent != cur:
            cur = cur.parent
        repo = cur if (cur / ".git").exists() else Path(a.out)
        stat = git_diff_stat(repo)
        if stat:
            print(f"   变更量:\n{stat}")
        ok, msg = create_pr_branch(repo, a.branch)
        if not ok:
            print(f"⚠️ {msg}")
            return 2
        print(f"✅ {msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
