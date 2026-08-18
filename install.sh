#!/usr/bin/env bash
# data-agent-engine 一键安装
# 用法: ./install.sh [--sample|--real]    默认 --sample（样例模式，开箱即用）
set -euo pipefail
cd "$(dirname "$0")"

MODE="${1:---sample}"

echo "==> 1/4 检查 uv"
command -v uv >/dev/null 2>&1 || { echo "❌ 未安装 uv，请先安装: https://docs.astral.sh/uv/"; exit 1; }
echo "    uv: $(command -v uv)"

echo "==> 2/4 Python 环境与依赖（backend/.venv；首次安装需几分钟，取决于网络）"
uv run --project backend python -c "import mcp, yaml" 2>/dev/null \
  || uv pip install --python backend/.venv/bin/python -r backend/requirements.txt
echo "    依赖就绪 (mcp, pyyaml)"

if [ "$MODE" = "--sample" ]; then
  echo "==> 3/4 生成 order 主题样例库（15 张表，固定种子可复现）"
  rm -f backend/seed/sample.db
  uv run --project backend python -m seed.seed --out backend/seed/sample.db --seed 42
  echo "    样例库: backend/seed/sample.db"
else
  echo "==> 3/4 真实数仓模式"
  echo "    请配置 backend/.env（连接串/驱动），并运行 builder 生成本体（见 README「二次开发」）"
fi

echo "==> 4/4 DSH 自动配置（写 cordis.patch.yml + 建「数据助理」预设）"
uv run --project backend python dsh-side/setup_dsh.py --backend "$(pwd)/backend"

echo ""
echo "✅ 安装完成。"
echo "  1) 首次运行请执行: cd ~/.dsh/profiles/web && pnpm install   （安装 dsh-mcp-client 依赖）"
echo "  2) 重启 DSH（或等待 HMR 热生效）"
echo "  3) 新开会话，预设选择「数据助理」，提问示例见 README「示例提问」章节"
echo ""
echo "评测门禁: uv run --project backend python -m eval.eval"
