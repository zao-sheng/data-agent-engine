#!/usr/bin/env bash
# data-agent-engine 安装 / 更新 / 卸载
# 用法:
#   ./install.sh                安装（样例模式，开箱即用）
#   ./install.sh --real         安装（真实数仓模式：配置 .env + builder 生成本体）
#   ./install.sh update         更新（git pull → 重建样例库 → 更新 DSH 配置 → 评测）
#   ./install.sh uninstall      卸载（移除 DSH 配置/预设/样例库/venv，源码保留）
set -euo pipefail
cd "$(dirname "$0")"

check_uv() {
  command -v uv >/dev/null 2>&1 || { echo "❌ 未安装 uv，请先安装: https://docs.astral.sh/uv/"; exit 1; }
}

do_env() {
  echo "==> Python 环境与依赖（backend/.venv；首次安装需几分钟）"
  uv run --project backend python -c "import mcp, yaml" 2>/dev/null \
    || uv pip install --python backend/.venv/bin/python -r backend/requirements.txt
}

do_sample() {
  echo "==> 重建 order 主题样例库（15 张表，固定种子可复现）"
  rm -f backend/seed/sample.db
  uv run --project backend python -m seed.seed --out backend/seed/sample.db --seed 42
}

MODE="${1:-install}"
case "$MODE" in
  install|--sample|"")
    check_uv
    do_env
    do_sample
    uv run --project backend python dsh-side/setup_dsh.py --backend "$(pwd)/backend"
    echo ""
    echo "✅ 安装完成。"
    echo "  1) 首次运行请执行: cd ~/.dsh/profiles/web && pnpm install   （安装 dsh-mcp-client 依赖）"
    echo "  2) 重启 DSH（或等待 HMR 热生效）"
    echo "  3) 新开会话，预设选择「数据助理」，提问示例见 README「示例提问」"
    echo ""
    echo "评测门禁: uv run --project backend python -m eval.eval"
    ;;
  --real|real)
    check_uv
    do_env
    uv run --project backend python dsh-side/setup_dsh.py --backend "$(pwd)/backend"
    echo ""
    echo "✅ 真实数仓模式配置完成。"
    echo "  请配置 backend/.env（连接串/驱动），运行 builder 生成本体（见 README「二次开发」）"
    ;;
  update)
    echo "==> 更新代码（git pull，若失败请手动更新到最新版）"
    git pull 2>/dev/null && echo "    git pull 完成" || echo "    ⚠️ 非 git 仓库或 pull 失败，请手动更新代码后重试"
    check_uv
    do_env
    do_sample
    uv run --project backend python dsh-side/setup_dsh.py --backend "$(pwd)/backend" --mode update
    echo ""
    echo "==> 评测门禁"
    uv run --project backend python -m eval.eval || echo "⚠️ 评测未通过，请检查更新"
    echo ""
    echo "✅ 更新完成。请重启 DSH（或等待 HMR 热生效）后验证。"
    ;;
  uninstall)
    echo "==> 移除 DSH 配置（patch 块 + 数据助理预设）"
    uv run --project backend python dsh-side/setup_dsh.py --mode uninstall
    echo ""
    echo "==> 清理数据文件"
    rm -f backend/seed/sample.db && echo "    已删除样例库 backend/seed/sample.db"
    rm -rf backend/.venv && echo "    已删除虚拟环境 backend/.venv"
    echo ""
    echo "✅ 卸载完成。源码保留（backend/ontology 等仓库文件未动）。"
    echo "  如需彻底移除 dsh-mcp-client 依赖（仅当无其他插件使用）："
    echo "    cd ~/.dsh/profiles/web && pnpm remove @deepseek-ai/dsh-mcp-client"
    ;;
  *)
    echo "用法: ./install.sh [install|--sample|--real|update|uninstall]"
    exit 1
    ;;
esac
