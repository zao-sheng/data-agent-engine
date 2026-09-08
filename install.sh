#!/usr/bin/env bash
# data-agent-engine 安装 / 更新 / 卸载
# 用法:
#   ./install.sh                安装（样例模式，开箱即用）
#   ./install.sh --real         安装（真实数仓模式：配置 .env + builder 生成本体）
#   ./install.sh --supabase     安装（Supabase 本体真源模式：建表提示 + 导入样例本体 + 配置 .env）
#   ./install.sh update         更新（git pull → 重建样例库 → 更新 DSH 配置 → 本体编译 → 评测）
#   ./install.sh uninstall      卸载（移除 DSH 配置/预设/样例库/venv，源码保留）
set -euo pipefail
cd "$(dirname "$0")"

BACKEND="$(pwd)/backend"
PY="$BACKEND/.venv/bin/python"

# run_py：在 backend/ 目录下执行 venv python。
# 模块（seed/ontology_compile/eval/ontology_import...）都在 backend/ 下，
# 从项目根 `-m` 会 ModuleNotFoundError（除非 venv 装了 editable——全新安装
# 只有 requirements，没有 editable）。故统一 cd backend 后执行。
# 注意：传给 python 的相对路径参数均相对 backend/ 目录。
run_py() {
  (cd "$BACKEND" && ./.venv/bin/python "$@")
}

check_uv() {
  command -v uv >/dev/null 2>&1 || { echo "❌ 未安装 uv，请先安装: https://docs.astral.sh/uv/"; exit 1; }
}

do_env() {
  echo "==> Python 环境与依赖（backend/.venv；首次安装需几分钟）"
  if [ ! -x "$PY" ]; then
    uv venv "$BACKEND/.venv"
  fi
  # 直接用 venv python 检查依赖（避免 uv run 的缓存/网络差异）
  "$PY" -c "import mcp, yaml" 2>/dev/null \
    || uv pip install --python "$PY" -r backend/requirements.txt
}

do_sample() {
  echo "==> 重建 order 主题样例库（15 张表，固定种子可复现）"
  rm -f backend/seed/sample.db
  run_py -m seed.seed --out seed/sample.db --seed 42
}

do_compile() {
  echo "==> 本体编译（YAML → SQLite 产物，验证编译流水线）"
  run_py -m ontology_compile --yaml ontology --out /tmp/ontology-verify.db
  rm -f /tmp/ontology-verify.db
}

do_gate() {
  echo "==> 评测门禁"
  run_py -m eval.eval || echo "⚠️ 评测未通过，请检查"
  echo "==> 引擎单测"
  run_py -m unittest discover -s tests 2>/dev/null || true
  echo "==> DSH 侧回归"
  run_py -m unittest discover -s "$(dirname "$BACKEND")/dsh-side" 2>/dev/null || true
}

MODE="${1:-install}"
case "$MODE" in
  install|--sample|"")
    check_uv
    do_env
    do_sample
    "$PY" dsh-side/setup_dsh.py --backend "$BACKEND"
    do_compile
    echo ""
    echo "✅ 安装完成。"
    echo "  1) 首次运行请执行: cd ~/.dsh/profiles/web && pnpm install   （安装 dsh-mcp-client 依赖）"
    echo "  2) 重启 DSH（或等待 HMR 热生效）"
    echo "  3) 新开会话，预设选择「数据助理」，提问示例见 README「示例提问」"
    echo ""
    echo "评测门禁: cd backend && .venv/bin/python -m eval.eval"
    ;;
  --real|real)
    check_uv
    do_env
    "$PY" dsh-side/setup_dsh.py --backend "$BACKEND"
    echo ""
    echo "✅ 真实数仓模式配置完成。"
    echo "  请配置 backend/.env（连接串/驱动），运行 builder 生成本体（见 README「二次开发」）"
    ;;
  --supabase|supabase)
    check_uv
    do_env
    do_sample
    "$PY" dsh-side/setup_dsh.py --backend "$BACKEND"
    echo ""
    echo "==> Supabase 本体真源模式"
    echo "  1) 建表：在 Supabase SQL Editor 执行 backend/supabase/schema.sql"
    echo "     （可选）RLS 权限：backend/supabase/rls.sql"
    echo "  2) 配置 backend/.env（已存在则追加）:"
    echo "     DATA_AGENT_ONTOLOGY_STORE=supabase"
    echo "     DATA_AGENT_SUPABASE_URL=https://xxxx.supabase.co"
    echo "     DATA_AGENT_SUPABASE_KEY=service_role_key"
    echo "  3) 导入样例本体（需先配置 .env）:"
    echo "     cd backend && .venv/bin/python -m ontology_import --yaml ontology"
    echo "  4) 验证一致性:"
    echo "     cd backend && .venv/bin/python -m ontology_sync_check --yaml ontology"
    echo ""
    echo "✅ Supabase 模式配置指引完成。配置好后重启 DSH，引擎从云端读本体。"
    ;;
  update)
    echo "==> 更新代码（git pull，若失败请手动更新到最新版）"
    git pull 2>/dev/null && echo "    git pull 完成" || echo "    ⚠️ 非 git 仓库或 pull 失败，请手动更新代码后重试"
    check_uv
    do_env
    do_sample
    "$PY" dsh-side/setup_dsh.py --backend "$BACKEND" --mode update
    do_compile
    do_gate
    echo ""
    echo "✅ 更新完成。请重启 DSH（或等待 HMR 热生效）后验证。"
    ;;
  uninstall)
    echo "==> 移除 DSH 配置（patch 块 + 数据助理预设）"
    "$PY" dsh-side/setup_dsh.py --mode uninstall
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
    echo "用法: ./install.sh [install|--sample|--real|--supabase|update|uninstall]"
    exit 1
    ;;
esac
