"""DSH 自动配置：写 cordis.patch.yml（MCP 接入）+ 建「数据助理」预设。

三种模式（均幂等）：
  install   —— 首次安装：对齐插件依赖 + upsert patch 块 + 创建预设
  update    —— 更新：对齐插件依赖 + upsert patch（backend 路径可能变化）
               + 备份并覆盖预设（DSH 升级会重置 profile 配置，用此模式修复）
  uninstall —— 卸载：移除 patch 块 + 删除预设（源码/样例库/venv 由 install.sh 清理）

用法：
  python dsh-side/setup_dsh.py --backend /abs/path/to/backend            # install
  python dsh-side/setup_dsh.py --backend /abs/path/to/backend --mode update
  python dsh-side/setup_dsh.py --mode uninstall
首次安装后需手动执行一次：dsh plugin --profile <profile> install
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HOME = Path.home()
SRC_PRESET = Path(__file__).resolve().parent / "agent-presets" / "data-agent"
DST_PRESET = HOME / ".dsh" / ".agent-presets" / "data-agent"

# DSH 0.1.5+ 的 cordis.patch.yml 默认内容是注释 + 空数组占位 `[]`
_EMPTY_LIST_RE = re.compile(r"(?m)^[ \t]*\[\][ \t]*(?:#.*)?\n?")
# 顶层 patch 列表项（`- ...` 顶格），用于判断移除后是否还有内容
_LIST_ENTRY_RE = re.compile(r"(?m)^- ")


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


def _has_list_entry(text: str) -> bool:
    """text 中是否还有顶层 patch 列表项（`- ...` 顶格，非缩进非注释）"""
    return bool(_LIST_ENTRY_RE.search(text))


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
    """写入或替换 data-agent patch 块（幂等，backend 路径变化时更新）

    兼容 DSH 0.1.5+ 的 patch 文件初始内容：新版 `cordis.patch.yml` 默认为
    注释 + 空数组占位 `[]`（旧版只有注释）。直接 append 会得到
    `[]\\n- insert:` 这种非法 YAML，因此需要识别 `[]` 占位行并就地替换；
    空数组也被 YAML 解析为「无 patch」，替换后语义等价。
    """
    patch = profile / "cordis.patch.yml"
    text = patch.read_text() if patch.exists() else "# dsh profile patch layer\n"
    block = _patch_block(backend)
    r = _block_range(text)
    if r:
        s, e = r
        text = text[:s] + block + text[e:]
        print(f"✅ 已更新 {patch} 中的 data-agent 块")
    elif _EMPTY_LIST_RE.search(text):
        # 替换空数组占位，避免 `[]` 后面直接跟列表项造成非法 YAML
        text = _EMPTY_LIST_RE.sub(block.rstrip("\n"), text, count=1)
        print(f"✅ 已写入 {patch}（替换空数组占位）")
    else:
        text = text.rstrip() + "\n" + block
        print(f"✅ 已写入 {patch}")
    patch.write_text(text)


def remove_patch(profile: Path) -> None:
    """移除 data-agent patch 块（幂等）

    移除后若文件只剩注释/空行，补回 `[]` 占位，保持与 DSH 新版默认一致的
    合法空 patch 形态。
    """
    patch = profile / "cordis.patch.yml"
    if not patch.exists():
        print("ℹ️  无 patch 文件，跳过")
        return
    text = patch.read_text()
    r = _block_range(text)
    if r:
        s, e = r
        text = (text[:s] + text[e:]).rstrip() + "\n"
        if not _has_list_entry(text):
            text = text.rstrip() + "\n[]\n"
        patch.write_text(text)
        print(f"✅ 已从 {patch} 移除 data-agent 块")
    else:
        print("ℹ️  patch 中无 data-agent 块，跳过")


def _mcp_client_resolvable(profile: Path) -> bool:
    """`@deepseek-ai/dsh-mcp-client` 是否已由 DSH 运行时自带（无需 profile 声明）。

    DSH 自身把 dsh-mcp-client 列为直接依赖（0.1.5-rc.1 起），profile 引导时
    在 `~/.dsh/profiles/node_modules/@deepseek-ai/` 建立指向 dsh 安装目录的
    符号链接；Node 从 profile 目录向上查找即可命中。因此**常规安装无需任何
    `dsh plugin install`**，也不需要 profile dependencies 条目。
    """
    return (profile / "node_modules" / "@deepseek-ai" / "dsh-mcp-client").exists() or (
        profile.parent / "node_modules" / "@deepseek-ai" / "dsh-mcp-client"
    ).exists()


def _dsh_version() -> str | None:
    """当前 dsh 版本（用于给插件依赖锁定与运行时一致的 spec）。"""
    exe = shutil.which("dsh")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    return lines[0] if lines else None


def ensure_mcp_client_dep(profile: Path) -> bool:
    """对齐 dsh-mcp-client 依赖声明（幂等）。返回是否**需要**用户执行安装。

    DSH 0.1.5+ 语义（两个都要遵守，否则 MCP 挂不上）：
    1. `dsh-mcp-client` 是**树外插件**，只能出现在 profile `package.json`
       的 `dependencies`；**不能**写进 `dsh.profile.bundles`——bundles 是
       **组合包**列表（提供 patch 层的包，如 dsh-base / dsh-web-app）。
       历史版本写错的 bundles 条目在此清理。
    2. DSH 运行时**自带** dsh-mcp-client。若它已可从 profile 解析到，则
       profile 里不应再声明（旧版脚本写的 `latest` 会与运行时版本漂移，
       经 pnpm 安装后反而覆盖自带副本）——此时删除该声明并返回 False。
       只有在解析不到时（罕见：dsh 自带依赖缺失）才声明为 `^<dsh 版本>`，
       并返回 True 提示用户执行 `dsh plugin --profile <name> install`。
    """
    mcp = "@deepseek-ai/dsh-mcp-client"
    pkg = profile / "package.json"
    if not pkg.exists():
        print(f"⚠️  未找到 profile package.json，请手动确认依赖 {mcp}")
        return False
    data = json.loads(pkg.read_text())
    deps = data.setdefault("dependencies", {})
    # 只读取，不因缺失而创建 dsh.profile.bundles 结构（避免无谓改写）
    bundles = data.get("dsh", {}).get("profile", {}).get("bundles", [])
    changed = False

    # (1) 清理历史版本错误写入 bundles 的条目（插件 ≠ 组合包）
    if mcp in bundles:
        bundles.remove(mcp)
        data["dsh"]["profile"]["bundles"] = bundles
        changed = True
        print(f"✅ 已从 dsh.profile.bundles 移除 {mcp}（插件不是组合包）")

    resolvable = _mcp_client_resolvable(profile)
    need_install = False
    if resolvable:
        # (2) 运行时自带：移除 profile 依赖声明，避免版本漂移
        if mcp in deps:
            del deps[mcp]
            changed = True
            print(f"✅ 已移除 profile 依赖声明 {mcp}（DSH 运行时自带，无需重复安装）")
        else:
            print(f"ℹ️  {mcp} 由 DSH 运行时提供，无需安装")
    else:
        ver = _dsh_version()
        want = f"^{ver}" if ver else "latest"
        if deps.get(mcp) != want:
            deps[mcp] = want
            changed = True
        need_install = True
        print(f"⚠️  未解析到 {mcp}，已在 dependencies 声明 {want}")

    if changed:
        pkg.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return need_install


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
        print("  rm -f backend/seed/sample.db && rm -rf backend/.venv   # 或运行 ./install.sh uninstall")
        return

    if not a.backend:
        raise SystemExit("install/update 需要 --backend 参数")
    backend = str(Path(a.backend).resolve())
    if not (Path(backend) / "mcp_servers" / "server.py").exists():
        raise SystemExit(f"{backend} 不是有效 backend 目录（缺少 mcp_servers/server.py）")

    need_install = ensure_mcp_client_dep(profile)
    upsert_patch(profile, backend)
    ensure_preset(backend, a.mode)
    print()
    if a.mode == "update":
        print("✅ 更新完成。请重启 DSH（或等待 HMR 热生效）后验证。")
        if need_install:
            print(f"⚠️  还需安装插件依赖: dsh plugin --profile {profile.name} install")
    else:
        print("下一步：")
        if need_install:
            print(f"  1) 安装插件依赖: dsh plugin --profile {profile.name} install")
            print("  2) 重启 DSH（或等待 HMR 热生效）")
        else:
            print("  1) 重启 DSH（或等待 HMR 热生效）")
        print(f"  {'3' if need_install else '2'}) 新开会话，预设选择「数据助理」，提问即可")


if __name__ == "__main__":
    sys.exit(main())
