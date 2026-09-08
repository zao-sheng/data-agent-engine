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

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from setup_dsh import _block_range, _patch_block

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


if __name__ == "__main__":
    unittest.main()
