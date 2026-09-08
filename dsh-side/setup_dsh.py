"""DSH 自动配置：写 cordis.patch.yml（MCP 接入）+ 建「数据助理」预设。

三种模式（均幂等）：
  install   —— 首次安装：补依赖 + upsert patch 块 + 创建预设
  update    —— 更新：upsert patch（backend 路径可能变化）+ 备份并覆盖预设
  uninstall —— 卸载：移除 patch 块 + 删除预设（源码/样例库/venv 由 install.sh 清理）

用法：
  python dsh-side/setup_dsh.py --backend /abs/path/to/backend            # install
  python dsh-side/setup_dsh.py --backend /abs/path/to/backend --mode update
  python dsh-side/setup_dsh.py --mode uninstall
首次安装后需手动执行一次：cd ~/.dsh/profiles/<profile> && pnpm install
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

HOME = Path.home()
SRC_PRESET = Path(__file__).resolve().parent / "agent-presets" / "data-agent"
DST_PRESET = HOME / ".dsh" / ".agent-presets" / "data-agent"


def _block_range(text: str) -> tuple[int, int] | None:
    """data-agent 块的范围（字符偏移 [start, end)）。行级扫描，兼容新旧块格式：
    - 起始标记：# --- data-agent engine
    - 块首行（紧随的 '- id: mcp-data-agent'）属于块
    - 结束：# --- end data-agent engine 标记行，或下一个【非缩进】行（旧块无 end），或文件尾"""
    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line)
    start = None
    for i, line in enumerate(lines):
        if line.startswith("# --- data-agent engine"):
            start = i
        elif start is not None:
            if line.startswith("# --- end data-agent engine"):
                return (offsets[start], pos if i + 1 >= len(lines) else offsets[i + 1])
            if i > start + 1 and line.strip() and not line.startswith((" ", "\t")):
                return (offsets[start], offsets[i])
    return (offsets[start], len(text)) if start is not None else None


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


def _patch_block(backend: str) -> str:
    """data-agent patch 块。

    注意：
    1. patch 顶层条目的语义是「覆盖已存在的行」——目标 id 不存在时会被
       loader 警告并静默跳过（entry "xxx" not found），新增插件实例必须用
       `- insert:` 列表包裹（见 dsh-app-boot applyEntryPatches）。
    2. mcp-client 的 stdio spawn 使用 host 进程的 cwd（DSH profile 目录，
       不是 backend），所以脚本路径必须绝对化，并显式指定 cwd 指向
       backend，避免 'mcp_servers/server.py' 找不到（Errno 2）。
    3. 直接用 backend 的 venv python 启动（install.sh 已建好 .venv），
       不用 `uv run`——uv run 依赖缓存目录可写且会重新解析依赖，
       全新/受限环境可能失败；venv python 零额外依赖、克隆即跑。
    """
    py = str(Path(backend) / ".venv" / "bin" / "python")
    server = str(Path(backend) / "mcp_servers" / "server.py")
    return f"""# --- data-agent engine (auto-managed by setup_dsh.py) ---
- insert:
    - id: mcp-data-agent
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: dataagent
        transport: stdio
        command: {py}
        args: ['{server}']
        cwd: '{backend}'
        env: {{ PYTHONUNBUFFERED: '1' }}
        toolCallTimeoutMs: 120000
# --- end data-agent engine ---
"""


def upsert_patch(profile: Path, backend: str) -> None:
    """写入或替换 data-agent patch 块（幂等，backend 路径变化时更新）"""
    patch = profile / "cordis.patch.yml"
    text = patch.read_text() if patch.exists() else "# dsh profile patch layer\n"
    block = _patch_block(backend)
    r = _block_range(text)
    if r:
        s, e = r
        text = text[:s] + block + text[e:]
        print(f"✅ 已更新 {patch} 中的 data-agent 块")
    else:
        text = text.rstrip() + "\n" + block
        print(f"✅ 已写入 {patch}")
    patch.write_text(text)


def remove_patch(profile: Path) -> None:
    """移除 data-agent patch 块（幂等）"""
    patch = profile / "cordis.patch.yml"
    if not patch.exists():
        print("ℹ️  无 patch 文件，跳过")
        return
    text = patch.read_text()
    r = _block_range(text)
    if r:
        s, e = r
        patch.write_text((text[:s] + text[e:]).rstrip() + "\n")
        print(f"✅ 已从 {patch} 移除 data-agent 块")
    else:
        print("ℹ️  patch 中无 data-agent 块，跳过")


def ensure_mcp_client_dep(profile: Path) -> None:
    """添加 dsh-mcp-client 依赖 + bundle（幂等）"""
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


def ensure_preset(backend: str, mode: str) -> None:
    if mode == "uninstall":
        if DST_PRESET.exists():
            shutil.rmtree(DST_PRESET)
            print(f"✅ 已删除预设: {DST_PRESET}")
        else:
            print("ℹ️  预设不存在，跳过")
        return

    if DST_PRESET.exists() and mode == "update":
        # 备份旧预设（含用户可能的自定义），再覆盖
        bak = DST_PRESET.with_name(DST_PRESET.name + f".bak-{datetime.now():%Y%m%d%H%M%S}")
        shutil.copytree(DST_PRESET, bak)
        print(f"ℹ️  旧预设已备份到: {bak}")
        shutil.rmtree(DST_PRESET)
    elif DST_PRESET.exists():
        print(f"ℹ️  预设已存在: {DST_PRESET}（更新请用 --mode update）")
        return

    DST_PRESET.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SRC_PRESET, DST_PRESET)
    # 替换 persona 中的占位路径
    yml = DST_PRESET / "agent.cordis.yml"
    yml.write_text(yml.read_text().replace("{{BACKEND}}", backend))
    print(f"✅ 预设已就位: {DST_PRESET}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", help="backend 目录绝对路径（install/update 需要）")
    ap.add_argument("--mode", choices=["install", "update", "uninstall"], default="install")
    a = ap.parse_args()

    profile = find_profile()
    print(f"使用 profile: {profile}")

    if a.mode == "uninstall":
        remove_patch(profile)
        ensure_preset("", "uninstall")
        print()
        print("已卸载 DSH 配置。如需清理数据文件与依赖：")
        print("  rm -f backend/seed/sample.db backend/.venv -rf   # 或运行 ./install.sh uninstall")
        print("  cd " + str(profile) + " && pnpm remove @deepseek-ai/dsh-mcp-client   # 仅当无其他插件使用")
        return

    if not a.backend:
        raise SystemExit("install/update 需要 --backend 参数")
    backend = str(Path(a.backend).resolve())
    if not (Path(backend) / "mcp_servers" / "server.py").exists():
        raise SystemExit(f"{backend} 不是有效 backend 目录（缺少 mcp_servers/server.py）")

    if a.mode == "install":
        ensure_mcp_client_dep(profile)
    upsert_patch(profile, backend)
    ensure_preset(backend, a.mode)
    print()
    if a.mode == "update":
        print("✅ 更新完成。请重启 DSH（或等待 HMR 热生效）后验证。")
    else:
        print("下一步：")
        print("  1) 首次运行请执行: cd " + str(profile) + " && pnpm install")
        print("  2) 重启 DSH（或等待 HMR 热生效）")
        print("  3) 新开会话，预设选择「数据助理」，提问即可")


if __name__ == "__main__":
    sys.exit(main())
