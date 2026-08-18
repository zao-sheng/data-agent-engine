"""DSH 自动配置：写 cordis.patch.yml（MCP 接入）+ 建「数据助理」预设（幂等）。

用法：python dsh-side/setup_dsh.py --backend /abs/path/to/backend
首次运行后需手动执行一次：cd ~/.dsh/profiles/<profile> && pnpm install
（脚本不代跑，避免半途失败；mcp-client 依赖只有首次需要装）
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

HOME = Path.home()


def find_profile() -> Path:
    """探测用户 profile：优先 web，否则取第一个带 cordis.patch.yml 的"""
    roots = sorted((HOME / ".dsh" / "profiles").glob("*"))
    for r in roots:
        if r.name == "web" and (r / "cordis.patch.yml").exists():
            return r
    for r in roots:
        if (r / "cordis.patch.yml").exists():
            return r
    raise SystemExit("未找到 ~/.dsh/profiles/* 下的 profile，请检查 DSH 安装")


def ensure_patch(profile: Path, backend: str) -> None:
    patch = profile / "cordis.patch.yml"
    uv = shutil.which("uv") or "uv"
    block = f"""# --- data-agent engine (auto-managed by setup_dsh.py) ---
- id: mcp-data-agent
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: dataagent
    transport: stdio
    command: {uv}
    args: ['run', '--project', '{backend}', 'python', 'mcp_servers/server.py']
    env: {{ PYTHONUNBUFFERED: '1' }}
    toolCallTimeoutMs: 120000
"""
    text = patch.read_text() if patch.exists() else "# dsh profile patch layer\n"
    if "mcp-data-agent" not in text:
        patch.write_text(text.rstrip() + "\n" + block)
        print(f"✅ 已写入 {patch}")
    else:
        print(f"ℹ️  {patch} 已含 mcp-data-agent，跳过")


def ensure_mcp_client_dep(profile: Path) -> None:
    pkg = profile / "package.json"
    if not pkg.exists():
        print("⚠️  未找到 profile package.json，请手动在 dependencies 添加 @deepseek-ai/dsh-mcp-client")
        return
    data = json.loads(pkg.read_text())
    deps = data.setdefault("dependencies", {})
    bundles = data.setdefault("dsh", {}).setdefault("profile", {}).setdefault("bundles", [])
    changed = False
    if "@deepseek-ai/dsh-mcp-client" not in deps:
        deps["@deepseek-ai/dsh-mcp-client"] = "latest"
        changed = True
    if "@deepseek-ai/dsh-mcp-client" not in bundles:
        bundles.append("@deepseek-ai/dsh-mcp-client")
        changed = True
    if changed:
        pkg.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        print("✅ profile package.json 已加入 dsh-mcp-client 依赖 + bundle")
        print("   请手动执行: cd " + str(profile) + " && pnpm install")
    else:
        print("ℹ️  dsh-mcp-client 已在依赖中，跳过")


def ensure_preset(backend: str) -> Path:
    src = Path(__file__).resolve().parent / "agent-presets" / "data-agent"
    dst = HOME / ".dsh" / ".agent-presets" / "data-agent"
    if dst.exists():
        print(f"ℹ️  预设已存在: {dst}（如需更新请先删除）")
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst)
        print(f"✅ 已创建预设: {dst}")
    # 替换 persona 中的占位路径
    yml = dst / "agent.cordis.yml"
    text = yml.read_text().replace("{{BACKEND}}", backend)
    yml.write_text(text)
    return dst


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", required=True, help="backend 目录绝对路径（含 core/、mcp_servers/）")
    a = ap.parse_args()
    backend = str(Path(a.backend).resolve())
    if not (Path(backend) / "mcp_servers" / "server.py").exists():
        raise SystemExit(f"{backend} 不是有效 backend 目录（缺少 mcp_servers/server.py）")

    profile = find_profile()
    print(f"使用 profile: {profile}")
    ensure_mcp_client_dep(profile)
    ensure_patch(profile, backend)
    ensure_preset(backend)
    print()
    print("下一步：")
    print("  1) 首次运行请执行: cd " + str(profile) + " && pnpm install")
    print("  2) 重启 DSH（或等待 HMR 热生效）")
    print("  3) 新开会话，预设选择「数据助理」，提问即可")


if __name__ == "__main__":
    sys.exit(main())
