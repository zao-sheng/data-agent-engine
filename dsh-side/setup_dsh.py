"""DSH 自动配置：写 cordis.patch.yml（MCP 接入 + 「数据助理」预设声明）。

三种模式（均幂等）：
  install   —— 首次安装：对齐插件依赖 + upsert patch 块 + 创建预设
  update    —— 更新：对齐插件依赖 + upsert patch（backend 路径可能变化）
               + 备份并覆盖预设（DSH 升级会重置 profile 配置，用此模式修复）
  uninstall —— 卸载：移除 patch 块 + 删除预设（源码/样例库/venv 由 install.sh 清理）

预设安装方式按运行时能力自动选择：
  * DSH 0.1.7+：声明式——patch 里写 `@deepseek-ai/dsh-agent-preset` 行，并把
    运行时自带的该包软链到 ~/.dsh/profiles/node_modules（profile patch 的
    包名按 Node 规则从 profile 目录向上解析，不软链会 MODULE_NOT_FOUND）；
    旧的 ~/.dsh/.agent-presets/ 目录从此不再被读取。
  * DSH 0.1.6 及更早：目录式——复制到 ~/.dsh/.agent-presets/data-agent。

用法：
  python dsh-side/setup_dsh.py --backend /abs/path/to/backend            # install
  python dsh-side/setup_dsh.py --backend /abs/path/to/backend --mode update
  python dsh-side/setup_dsh.py --mode uninstall
首次安装后需手动执行一次：dsh plugin --profile <profile> install
"""
from __future__ import annotations

import argparse
import json
import os
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

# ── DSH 0.1.7+ 的声明式预设（插件组合包）──────────────────────────────
# 0.1.7 起 Agent 预设不再从 ~/.dsh/.agent-presets/<name>/ 目录加载，改由
# `@deepseek-ai/dsh-agent-preset` 行在 patch 里声明；旧的目录式预设不会被
# 任何代码读取（会静默消失，新建会话选不到「数据助理」）。
AGENT_PRESET_PKG = "@deepseek-ai/dsh-agent-preset"
AGENT_PRESET_NAME = "dsh-agent-preset"  # 文件系统查找用的短名
PRESET_ID = "data-agent"
_PRESET_BLOCK_BEGIN = "# --- data-agent preset (auto-managed by setup_dsh.py) ---"
_PRESET_BLOCK_END = "# --- end data-agent preset ---"
# 目录式预设用 baseUrl 相对解析 skills/；声明式 patch 里 baseUrl 指向 profile，
# 必须换成仓库内 skills 目录的绝对路径
_LEGACY_SKILLS_RE = re.compile(r"-\s*!!js\s*\"process\.getBuiltinModule\('node:url'\)[^\"]*\"")


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
    end = None
    for i, line in enumerate(lines):
        if line.startswith("# --- data-agent engine"):
            start = i
            continue
        if start is None:
            continue
        if line.startswith("# --- end data-agent engine"):
            end = pos if i + 1 >= len(lines) else offsets[i + 1]
            break
        # 首个条目（i == start + 1）之后的顶格非空行＝外来条目。DSH 0.1.7 的设置
        # 导入器会把 ui-theme / agent-default-model 等覆盖条目插进 marker 区间内，
        # 必须在它之前收口，否则 upsert 会用引擎块把用户设置一起覆盖掉。
        if i > start + 1 and line.strip() and not line.startswith((" ", "\t")):
            return (offsets[start], offsets[i])
    if start is None:
        return None
    return (offsets[start], end if end is not None else len(text))


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
       的 `dependencies`；**不能**写进 `dsh.profile.bundles`（0.0.x 的旧语义
       允许插件进 bundles，0.1.5+ 只接受提供 patch 层的**组合包**，如
       dsh-base / dsh-web-app）。旧脚本按旧语义写下的 bundles 条目在此清理。
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

    # (1) 清理旧语义写入 bundles 的条目（0.1.5+ 的 bundles 只接受组合包）
    if mcp in bundles:
        bundles.remove(mcp)
        data["dsh"]["profile"]["bundles"] = bundles
        changed = True
        print(f"✅ 已从 dsh.profile.bundles 移除 {mcp}（0.1.5+ 的 bundles 只收组合包）")

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


def _dsh_install_modules() -> Path | None:
    """dsh 安装目录下的 `@deepseek-ai` 目录（运行时自带包的真源）。

    与 test_setup_dsh.py 的 `_dsh_plugin_root()` 同一套探测逻辑：从 `which dsh`
    的真实路径向上找 name == `@deepseek-ai/dsh` 的 package.json。
    """
    exe = shutil.which("dsh")
    if not exe:
        return None
    real = Path(os.path.realpath(exe))
    for cand in [real.parent, *real.parents]:
        pkg = cand / "package.json"
        if not pkg.exists():
            continue
        try:
            if json.loads(pkg.read_text()).get("name") == "@deepseek-ai/dsh":
                return cand / "node_modules" / "@deepseek-ai"
        except (OSError, ValueError):
            continue
    return None


def _agent_preset_supported() -> bool:
    """运行时是否自带声明式预设插件（DSH 0.1.7+）。"""
    roots = _dsh_install_modules()
    return bool(roots and (roots / AGENT_PRESET_NAME).exists())


def _package_resolvable(profile: Path, pkg: str) -> Path | None:
    """按 Node 规则，profile patch 里的 `name:` 能否解析到 pkg。"""
    for base in (profile, profile.parent):
        cand = base / "node_modules" / "@deepseek-ai" / pkg
        if cand.exists():
            return cand
    return None


def _link_runtime_package(profile: Path, pkg: str) -> None:
    """把运行时自带的包投影到 profile 共享 node_modules（幂等）。

    0.1.7 的 profile patch 条目按 Node 规则从 profile 目录向上解析包名，而
    `@deepseek-ai/dsh-mcp-client`、`@deepseek-ai/dsh-agent-preset` 这类包只在
    dsh 安装目录里，不在 profile 链上——不建软链就会 `MODULE_NOT_FOUND`，
    插件被静默跳过（预设列表里看不到「数据助理」）。既有环境的
    `~/.dsh/profiles/node_modules/@deepseek-ai/dsh-mcp-client` 就是这个形态。
    """
    if _package_resolvable(profile, pkg):
        print(f"ℹ️  {pkg} 已可解析，跳过软链")
        return
    roots = _dsh_install_modules()
    src = roots / pkg if roots else None
    if not src or not src.exists():
        print(f"⚠️  未找到运行时自带的 {pkg}，预设行可能加载失败：请确认 DSH ≥ 0.1.7")
        return
    link = profile.parent / "node_modules" / "@deepseek-ai" / pkg
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(src)
    print(f"✅ 已建立软链: {link} -> {src}")


def _preset_rows(backend: str) -> str:
    """仓库预设行 → 缩进到 `plugins:` 下的 YAML 片段（含 {{BACKEND}} 替换）。"""
    rows = (SRC_PRESET / "agent.cordis.yml").read_text().replace("{{BACKEND}}", backend)
    rows = _LEGACY_SKILLS_RE.sub(f"- {SRC_PRESET / 'skills'}", rows)
    return "\n".join(
        "" if not line.strip() else " " * 10 + line
        for line in rows.rstrip("\n").split("\n")
    )


def _preset_block(backend: str) -> str:
    """data-agent 预设声明块（0.1.7+）。

    语义要点：
    1. 新插件实例必须包在 `- insert:` 里（同 _patch_block 的 2026-08-19 bug）；
    2. `default: data-agent` 覆盖 shipped 组合包的 agent-preset-registry 行——
       等价被 0.1.7 废弃的 settings.yaml `agent-presets.default`（该键不会
       被设置导入器迁移，必须在这里补回，否则默认模式仍会是 standard）；
    3. skills 用仓库内绝对路径，避免依赖已废弃的 ~/.dsh/.agent-presets 副本。
    """
    meta = (SRC_PRESET / "preset.yml").read_text()
    name = re.search(r"(?m)^name:[ \t]*(.+?)[ \t]*$", meta).group(1)
    desc = re.search(r"(?m)^description:[ \t]*(.+?)[ \t]*$", meta).group(1)
    return f"""{_PRESET_BLOCK_BEGIN}
- insert:
    - id: preset-{PRESET_ID}
      name: '{AGENT_PRESET_PKG}'
      config:
        id: {PRESET_ID}
        name: {name}
        order: 2
        description: {desc}
        plugins:
{_preset_rows(backend)}

# 默认 preset（等价旧 settings.yaml 的 agent-presets.default: data-agent）
- id: agent-preset-registry
  config:
    default: {PRESET_ID}
{_PRESET_BLOCK_END}
"""


def _preset_block_range(text: str) -> tuple[int, int] | None:
    """预设声明块的范围（含 end 标记行与其后的换行）。"""
    s = text.find(_PRESET_BLOCK_BEGIN)
    if s < 0:
        return None
    e = text.find(_PRESET_BLOCK_END, s)
    if e < 0:
        return (s, len(text))
    e += len(_PRESET_BLOCK_END)
    if text[e:e + 1] == "\n":
        e += 1
    return (s, e)


def upsert_preset_patch(profile: Path, backend: str) -> None:
    """写入/替换预设声明块（幂等，backend 或预设内容变化时更新）。"""
    patch = profile / "cordis.patch.yml"
    text = patch.read_text() if patch.exists() else ""
    block = _preset_block(backend)
    r = _preset_block_range(text)
    if r:
        s, e = r
        text = text[:s] + block + text[e:]
        print(f"✅ 已更新 {patch} 中的预设声明块")
    elif _EMPTY_LIST_RE.search(text):
        text = _EMPTY_LIST_RE.sub(block.rstrip("\n"), text, count=1)
        print(f"✅ 已写入 {patch}（替换空数组占位）")
    else:
        text = text.rstrip() + "\n" + block
        print(f"✅ 已写入 {patch}")
    patch.write_text(text)


def remove_preset_patch(profile: Path) -> None:
    """移除预设声明块（幂等）；只剩注释时补回 `[]` 占位。"""
    patch = profile / "cordis.patch.yml"
    if not patch.exists():
        print("ℹ️  无 patch 文件，跳过预设块")
        return
    text = patch.read_text()
    r = _preset_block_range(text)
    if not r:
        print("ℹ️  patch 中无预设声明块，跳过")
        return
    s, e = r
    text = (text[:s] + text[e:]).rstrip() + "\n"
    if not _has_list_entry(text):
        text = text.rstrip() + "\n[]\n"
    patch.write_text(text)
    print(f"✅ 已从 {patch} 移除预设声明块")


def ensure_preset(profile: Path, backend: str, mode: str) -> None:
    """安装/移除「数据助理」预设。

    DSH 0.1.7+：声明式（patch 里的 preset 行 + 运行时包软链）；
    0.1.6 及更早：目录式（复制到 ~/.dsh/.agent-presets/data-agent）。
    """
    if _agent_preset_supported():
        if mode == "uninstall":
            remove_preset_patch(profile)
        else:
            _link_runtime_package(profile, AGENT_PRESET_NAME)
            upsert_preset_patch(profile, backend)
        if DST_PRESET.exists() and mode == "uninstall":
            shutil.rmtree(DST_PRESET)
            print(f"✅ 已删除旧目录预设: {DST_PRESET}")
        elif DST_PRESET.exists():
            print(f"ℹ️  旧目录预设 {DST_PRESET} 在 0.1.7+ 已被忽略，"
                  f"可自行删除（声明式预设已生效）")
        return

    # ── 0.1.6 及更早：目录式 ─────────────────────────────────────────
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
        ensure_preset(profile, "", "uninstall")
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
    ensure_preset(profile, backend, a.mode)
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
