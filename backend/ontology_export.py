"""本体导出工具：Supabase（真源）→ YAML（Git 评审副本），支持一键 PR。

用法：
  # 仅导出 YAML（评审副本）
  python -m ontology_export --url <URL> --key <KEY> --out backend/ontology

  # 导出 + 建分支 + 提交 + 推送（Git 仓库内）
  python -m ontology_export --url <URL> --key <KEY> \
      --out backend/ontology --branch ontology/update-20260101

  # 导出 + 建分支 + 提交 + 推送 + 创建 PR（需 gh CLI）
  python -m ontology_export --url <URL> --key <KEY> \
      --out backend/ontology --branch b --pr \
      --title "本体更新: 新增XX指标" --body "变更说明..."

闭环：管理端直写 Supabase → 本工具导出 YAML → 提交 PR 评审 →
     评审通过合并（可选：ontology_compile 编译 SQLite 作为发布基线）。
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


def create_github_pr(repo: Path, branch: str, title: str, body: str) -> tuple[bool, str]:
    """用 gh CLI 创建 PR。返回 (ok, url_or_msg)。"""
    r = _run(["gh", "pr", "create", "--base", "main",
              "--head", branch, "--title", title, "--body", body], repo)
    if r.returncode != 0:
        return False, f"gh pr create 失败: {r.stderr.strip()}"
    return True, r.stdout.strip()


def main() -> int:
    ap = argparse.ArgumentParser(description="Supabase 本体 → YAML 导出（可一键 PR）")
    ap.add_argument("--url", default=None, help="Supabase URL（默认读环境变量）")
    ap.add_argument("--key", default=None, help="Supabase key（默认读环境变量）")
    ap.add_argument("--out", default="ontology", help="输出 YAML 目录")
    ap.add_argument("--schema", default="public")
    ap.add_argument("--branch", default=None, help="导出一并建分支提交推送（Git 评审）")
    ap.add_argument("--pr", action="store_true", help="推送后创建 PR（需 gh CLI + --branch）")
    ap.add_argument("--title", default="本体更新（来自 Supabase 导出）", help="PR 标题")
    ap.add_argument("--body", default="由 ontology_export 自动导出评审", help="PR 描述")
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

    # 2. 可选：建分支提交推送 + PR
    if a.branch:
        repo = Path(a.out).resolve().parent.parent  # ontology 目录的上级上级（仓库根）
        # 更稳：往上找 .git
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
            return 2 if a.pr else 0
        print(f"✅ {msg}")
        if a.pr:
            ok, msg2 = create_github_pr(repo, a.branch, a.title, a.body)
            if not ok:
                print(f"⚠️ {msg2}")
                return 3
            print(f"✅ PR 已创建: {msg2}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
