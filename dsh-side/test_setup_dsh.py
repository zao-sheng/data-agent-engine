"""setup_dsh.py 回归测试（重点：patch 块 insert 语义）。

背景 bug（2026-08-19 修复）：
  _patch_block 曾直接输出顶层条目 `- id: mcp-data-agent`。DSH loader
  （dsh-app-boot applyEntryPatches）把无 `insert` 的顶层条目解释为
  「覆盖已存在行」——组合树中不存在该 id 时警告 `entry "mcp-data-agent"
  not found` 并静默跳过，导致 MCP 插件从未加载、会话中没有任何
  mcp__dataagent__* 工具。新增插件实例必须包在 `- insert:` 列表里。

用法：uv run --project backend python -m pytest dsh-side/test_setup_dsh.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from setup_dsh import (
    _block_range,
    _patch_block,
    ensure_mcp_client_dep,
    remove_patch,
    upsert_patch,
)

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


class PatchBlockTest(unittest.TestCase):
    def setUp(self):
        self.backend = "/tmp/backend"

    def test_block_is_insert_list(self):
        """顶层必须是 - insert:，而不是裸条目（bug 根因）。"""
        block = _patch_block(self.backend)
        self.assertIn("- insert:", block)
        # insert 列表内的条目必须缩进
        for line in block.splitlines():
            if "id: mcp-data-agent" in line:
                self.assertTrue(line.startswith("    - id:"),
                                f"条目应在 insert 列表内缩进: {line!r}")

    def test_block_contains_server_config(self):
        block = _patch_block(self.backend)
        self.assertIn("serverName: dataagent", block)
        self.assertIn("transport: stdio", block)
        self.assertIn("mcp_servers/server.py", block)
        self.assertIn("toolCallTimeoutMs: 120000", block)

    def test_block_uses_absolute_script_path_and_cwd(self):
        """spawn 必须用绝对脚本路径 + cwd 指向 backend（host cwd 是 profile 目录，
        相对路径会 Errno 2 找不到 mcp_servers/server.py）。"""
        block = _patch_block(self.backend)
        self.assertIn(f"'{self.backend}/mcp_servers/server.py'", block)
        self.assertIn(f"cwd: '{self.backend}'", block)
        # 不允许出现裸相对脚本路径
        self.assertNotIn("'python', 'mcp_servers/server.py'", block)

    def test_block_uses_venv_python_not_uv_run(self):
        """直接用 backend/.venv/bin/python 启动，不用 uv run——
        uv run 依赖缓存目录可写且会重新解析依赖，全新/受限环境可能失败；
        venv 由 install.sh 建好，零额外依赖。"""
        block = _patch_block(self.backend)
        self.assertIn(f"command: {self.backend}/.venv/bin/python", block)
        self.assertNotIn("uv run", block)
        self.assertNotIn("'run'", block)

    @unittest.skipUnless(HAS_YAML, "需要 pyyaml 验证 YAML 可解析")
    def test_block_is_valid_yaml_with_insert_shape(self):
        """YAML 解析后应得到 [{insert: [{id, name, config}]}]。"""
        block = _patch_block(self.backend)
        docs = list(yaml.safe_load_all(block))
        self.assertEqual(len(docs), 1, "patch 块应只有一份文档")
        patches = docs[0]
        self.assertEqual(len(patches), 1)
        entry = patches[0]
        self.assertIn("insert", entry, "顶层必须是 insert 列表")
        self.assertEqual(len(entry["insert"]), 1)
        inner = entry["insert"][0]
        self.assertEqual(inner["id"], "mcp-data-agent")
        self.assertEqual(inner["name"], "@deepseek-ai/dsh-mcp-client")
        self.assertEqual(inner["config"]["serverName"], "dataagent")
        self.assertEqual(inner["config"]["transport"], "stdio")
        self.assertEqual(inner["config"]["cwd"], self.backend)
        self.assertIn("mcp_servers/server.py", inner["config"]["args"][-1])

    def test_block_range_roundtrip(self):
        """upsert 幂等：已有新格式块时替换为同格式块，范围正确。"""
        block = _patch_block(self.backend)
        text = "header\n" + block + "tail\n"
        r = _block_range(text)
        self.assertIsNotNone(r)
        s, e = r
        self.assertEqual(text[s:e], block)
        # 替换后再次识别仍然正确
        text2 = text[:s] + _patch_block("/other/backend") + text[e:]
        r2 = _block_range(text2)
        self.assertIsNotNone(r2)
        s2, e2 = r2
        self.assertIn("/other/backend", text2[s2:e2])

    def test_block_range_detects_old_format_too(self):
        """旧格式（裸条目，无 end 标记）也应能被识别并整体替换。"""
        old = (
            "# --- data-agent engine (auto-managed by setup_dsh.py) ---\n"
            "- id: mcp-data-agent\n"
            "  name: '@deepseek-ai/dsh-mcp-client'\n"
            "  config:\n"
            "    serverName: dataagent\n"
            "    transport: stdio\n"
            "    command: uv\n"
            "    args: ['run', '--project', '/tmp/backend', 'python', 'mcp_servers/server.py']\n"
            "    env: { PYTHONUNBUFFERED: '1' }\n"
            "    toolCallTimeoutMs: 120000\n"
            "other-top-level: 1\n"
        )
        r = _block_range(old)
        self.assertIsNotNone(r)
        s, e = r
        # 块应止于下一个非缩进顶层行（other-top-level）之前
        self.assertIn("mcp-data-agent", old[s:e])
        self.assertNotIn("other-top-level", old[s:e])
        self.assertEqual(old[e:].lstrip(), "other-top-level: 1\n")

    def test_block_range_detects_new_format_end_marker(self):
        """新格式块（带 end 标记）范围应到 end 标记为止。"""
        block = _patch_block(self.backend)
        text = "x\n" + block + "y\n"
        r = _block_range(text)
        s, e = r
        self.assertIn("# --- end data-agent engine", text[s:e])
        self.assertEqual(text[e:], "y\n")


class Dsh015CompatTest(unittest.TestCase):
    """DSH 0.1.5+ profile 布局兼容性回归。

    背景 bug（2026-09-10 修复）：升级 DSH 后 `dsh --profile web --dump-config`
    报 patch 解析失败、MCP 插件「均无法挂载」。两个根因：
      1) 新版 profile 的 cordis.patch.yml 初始内容由「注释」变为
         「注释 + 空数组 `[]`」。旧 upsert_patch 直接 append，产出
         `[]\\n- insert:` 这种非法 YAML，整份 patch 层解析失败 →
         该 profile 所有插件（不止 data-agent）都挂不上。
      2) ensure_mcp_client_dep 把 dsh-mcp-client 同时写进
         dsh.profile.bundles。新版 bundles 只接受「组合包」（提供 patch
         层的包，如 dsh-base/dsh-web-app）；插件应只进 dependencies。
    """

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.profile = Path(self._tmp.name)
        self.backend = "/tmp/backend"
        self.patch = self.profile / "cordis.patch.yml"
        self.pkg = self.profile / "package.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _patch_docs(self):
        """返回 patch 文件解析后的顶层列表（yaml.safe_load；空文件 → []）。"""
        return yaml.safe_load(self.patch.read_text()) or []

    def test_upsert_replaces_empty_list_placeholder(self):
        """新版默认的 `[]` 占位必须被就地替换，而不是在其后追加。"""
        self.patch.write_text("# dsh profile patch layer\n[]\n")
        upsert_patch(self.profile, self.backend)
        text = self.patch.read_text()
        # 不再有顶格的空数组行
        self.assertIsNone(_empty_line(text), f"仍存在空数组占位行:\n{text}")
        if HAS_YAML:
            docs = self._patch_docs()
            self.assertEqual(len(docs), 1, "patch 层应恰有一个顶层条目")
            self.assertIn("insert", docs[0])
            self.assertEqual(docs[0]["insert"][0]["id"], "mcp-data-agent")

    def test_upsert_appends_when_no_placeholder(self):
        """旧版注释-only 文件（无 `[]`）仍走追加路径，结果合法。"""
        self.patch.write_text("# dsh profile patch layer\n")
        upsert_patch(self.profile, self.backend)
        if HAS_YAML:
            docs = self._patch_docs()
            self.assertEqual(len(docs), 1)
            self.assertIn("insert", docs[0])

    def test_upsert_is_idempotent(self):
        """连续 upsert 不应重复插入条目（路径变化时更新原块）。"""
        self.patch.write_text("# dsh profile patch layer\n[]\n")
        upsert_patch(self.profile, self.backend)
        upsert_patch(self.profile, "/other/backend")
        text = self.patch.read_text()
        self.assertEqual(text.count("id: mcp-data-agent"), 1)
        self.assertIn("/other/backend", text)
        self.assertNotIn(self.backend, text, "旧路径应被替换")
        if HAS_YAML:
            docs = self._patch_docs()
            self.assertEqual(len(docs), 1)

    def test_remove_patch_restores_valid_empty_layer(self):
        """移除后文件仍须是可解析的空 patch 层（补回 `[]` 占位）。"""
        self.patch.write_text("# dsh profile patch layer\n[]\n")
        upsert_patch(self.profile, self.backend)
        remove_patch(self.profile)
        text = self.patch.read_text()
        self.assertNotIn("mcp-data-agent", text)
        if HAS_YAML:
            self.assertEqual(self._patch_docs(), [], "空 patch 层应解析为空列表")

    def test_ensure_dep_moves_plugin_out_of_bundles(self):
        """历史版本写进 bundles 的插件条目要被清理（插件 ≠ 组合包）。"""
        (self.profile / "node_modules" / "@deepseek-ai"
         / "dsh-mcp-client").mkdir(parents=True)
        self.pkg.write_text(json.dumps({
            "dependencies": {"@deepseek-ai/dsh-mcp-client": "latest"},
            "dsh": {"profile": {"bundles": [
                "@deepseek-ai/dsh-base",
                "@deepseek-ai/dsh-mcp-client",
            ]}},
        }))
        need_install = ensure_mcp_client_dep(self.profile)
        data = json.loads(self.pkg.read_text())
        self.assertFalse(need_install, "运行时自带插件 → 无需用户安装")
        self.assertNotIn("@deepseek-ai/dsh-mcp-client",
                         data["dsh"]["profile"]["bundles"])
        # 组合包不能被误删
        self.assertIn("@deepseek-ai/dsh-base", data["dsh"]["profile"]["bundles"])

    def test_ensure_dep_drops_declaration_when_runtime_provides_it(self):
        """DSH 运行时自带的插件不应再声明在 profile dependencies——
        旧脚本写的 `latest` 会与运行时版本漂移、安装后覆盖自带副本。"""
        (self.profile / "node_modules" / "@deepseek-ai"
         / "dsh-mcp-client").mkdir(parents=True)
        self.pkg.write_text(json.dumps({
            "dependencies": {"@deepseek-ai/dsh-mcp-client": "latest"},
            "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base"]}},
        }))
        self.assertFalse(ensure_mcp_client_dep(self.profile))
        data = json.loads(self.pkg.read_text())
        self.assertNotIn("@deepseek-ai/dsh-mcp-client", data["dependencies"])

    def test_ensure_dep_hoisted_runtime_link_counts_as_resolvable(self):
        """profile 上一级的 node_modules（DSH 提升安装位置）同样算可用。"""
        parent = self.profile.parent
        # setUp 用 TemporaryDirectory 作为 profile，这里模拟 ~/.dsh/profiles 布局
        self._tmp.cleanup()
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.profile = root / "web"
        self.profile.mkdir()
        self.pkg = self.profile / "package.json"
        (root / "node_modules" / "@deepseek-ai" / "dsh-mcp-client").mkdir(parents=True)
        self.pkg.write_text(json.dumps({
            "dependencies": {"@deepseek-ai/dsh-mcp-client": "latest"},
        }))
        self.assertFalse(ensure_mcp_client_dep(self.profile))
        data = json.loads(self.pkg.read_text())
        self.assertNotIn("@deepseek-ai/dsh-mcp-client", data["dependencies"])

    def test_ensure_dep_declares_pinned_spec_when_unresolvable(self):
        """解析不到插件时才声明依赖，且 spec 锁定到运行时版本（不用 latest）。"""
        self.pkg.write_text(json.dumps({
            "dependencies": {},
            "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base"]}},
        }))
        with mock.patch("setup_dsh._dsh_version", return_value="0.1.5-rc.1"):
            need_install = ensure_mcp_client_dep(self.profile)
        data = json.loads(self.pkg.read_text())
        self.assertTrue(need_install, "需提示用户执行 dsh plugin install")
        self.assertEqual(data["dependencies"]["@deepseek-ai/dsh-mcp-client"],
                         "^0.1.5-rc.1")
        self.assertEqual(data["dsh"]["profile"]["bundles"],
                         ["@deepseek-ai/dsh-base"])

    def test_ensure_dep_creates_no_bundles_key_when_absent(self):
        """package.json 无 dsh 段时不应凭空创建 dsh.profile.bundles。"""
        (self.profile / "node_modules" / "@deepseek-ai"
         / "dsh-mcp-client").mkdir(parents=True)
        self.pkg.write_text(json.dumps({"dependencies": {}}))
        ensure_mcp_client_dep(self.profile)
        data = json.loads(self.pkg.read_text())
        self.assertNotIn("dsh", data)


def _empty_line(text: str) -> str | None:
    """返回顶格 `[]` 占位行（含行尾换行），无则 None。"""
    for line in text.splitlines():
        if line.strip() == "[]":
            return line
    return None


# ── 预设（agent.cordis.yml）与已安装 DSH 插件 schema 的一致性 ──────────────

PRESET = (Path(__file__).resolve().parent / "agent-presets" / "data-agent"
          / "agent.cordis.yml")


if HAS_YAML:
    class _PresetLoader(yaml.SafeLoader):
        """允许 DSH 的 `!!js` 标签：求值发生在宿主，这里按字符串读入即可。"""

    _PresetLoader.add_constructor("tag:yaml.org,2002:js",
                                  lambda loader, node: loader.construct_scalar(node))


def _load_preset_rows() -> list[dict]:
    """解析预设组合文件为行列表。"""
    return yaml.load(PRESET.read_text(), Loader=_PresetLoader)


def _dsh_plugin_root() -> Path | None:
    """定位已安装 DSH 的插件目录（<dsh-root>/node_modules/@deepseek-ai）。"""
    exe = shutil.which("dsh")
    if not exe:
        return None
    for cand in [Path(os.path.realpath(exe)).parent, *Path(os.path.realpath(exe)).parents]:
        pkg = cand / "package.json"
        if not pkg.exists():
            continue
        try:
            if json.loads(pkg.read_text()).get("name") == "@deepseek-ai/dsh":
                return cand / "node_modules" / "@deepseek-ai"
        except (OSError, ValueError):
            continue
    return None


def _config_schema(plugin_root: Path, pkg: str) -> tuple[set[str], set[str]] | None:
    """从插件 lib/index.js 抽取 `const Config = z.object({...})` 的
    (全部字段, 必填字段)。抽不到返回 None（该行跳过校验）。"""
    src = plugin_root / pkg / "lib" / "index.js"
    if not src.exists():
        return None
    m = re.search(r"const Config = z\.object\(\{(.*?)\}\);", src.read_text(), re.S)
    if not m:
        return None
    all_keys: set[str] = set()
    required: set[str] = set()
    for line in m.group(1).splitlines():
        km = re.match(r"\s*([A-Za-z_$][\w$]*)\s*:\s*z\.", line)
        if not km:
            continue
        all_keys.add(km.group(1))
        if ".required()" in line:
            required.add(km.group(1))
    return (all_keys, required) if all_keys else None


@unittest.skipUnless(HAS_YAML, "需要 pyyaml 解析预设")
class PresetSchemaTest(unittest.TestCase):
    """预设行必须与已安装 DSH 的插件 config schema 一致。

    背景 bug（2026-09-15）：preset 的 persona 行写成 `text:`，而 0.1.5 的
    dsh-persona 字段是 `prefix`（required）——启动会话时报
    `$.prefix missing required value`，预设挂载失败，新建会话选「数据助理」
    直接报错。危害面被放大是因为 `install.sh update` 会把仓库源覆盖到
    ~/.dsh，源文件里的错键会原样打到用户环境。
    """

    def test_rows_have_id_and_name(self):
        rows = _load_preset_rows()
        self.assertTrue(rows, "预设不应为空")
        for row in rows:
            self.assertIn("id", row, row)
            self.assertTrue(str(row.get("name", "")).startswith("@deepseek-ai/"), row)

    def test_persona_uses_prefix_not_text(self):
        """dsh-persona 只认 prefix / suffix / complete / includeRuntimeContext。"""
        rows = [r for r in _load_preset_rows()
                if r["name"] == "@deepseek-ai/dsh-persona"]
        self.assertTrue(rows, "预设应有一行 persona")
        cfg = rows[0].get("config") or {}
        self.assertNotIn("text", cfg,
                         "dsh-persona 没有 text 字段（0.1.5 起为 prefix）")
        self.assertIn("prefix", cfg, "dsh-persona 的 prefix 是必填字段")
        self.assertTrue(str(cfg["prefix"]).strip(), "prefix 不能为空")

    def test_rows_only_use_declared_schema_keys(self):
        """逐行对照已安装插件 schema：字段名必须存在、必填项必须给全。"""
        root = _dsh_plugin_root()
        if root is None:
            self.skipTest("未安装 DSH，跳过 schema 对照")
        checked = 0
        for row in _load_preset_rows():
            pkg = str(row["name"]).split("/")[-1]
            schema = _config_schema(root, pkg)
            if schema is None:
                continue
            all_keys, required = schema
            cfg = row.get("config") or {}
            unknown = set(cfg) - all_keys
            self.assertEqual(
                unknown, set(),
                f"{pkg} 使用了 schema 中不存在的字段 {sorted(unknown)}；"
                f"合法字段: {sorted(all_keys)}")
            missing = required - set(cfg)
            self.assertEqual(
                missing, set(), f"{pkg} 缺少必填字段 {sorted(missing)}")
            checked += 1
        self.assertGreater(checked, 0, "应至少校验一行（persona）")


if __name__ == "__main__":
    unittest.main()
