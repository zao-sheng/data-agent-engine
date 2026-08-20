"""启动自检（P2）：server 启动时校验关键依赖，快速失败而非等到首次调用。

检查项：
  1. Ontology 目录存在且可解析（objects/functions/relations）
  2. 指标表非空（引擎可用的最小集）
  3. 数据介质可达（sqlite 样例库可读 / 远程方言已配置连接串）
  4. 日志目录可写

返回结构化检查报告；任一「必需项」失败时给出明确错误，帮助快速定位
配置问题（这是企业落地的 fail-fast 要求：启动即报错，而不是第一次
取数时才炸）。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .config import CONFIG


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    required: bool = True


def _check_ontology(onto) -> CheckResult:
    try:
        n_obj = len(onto.objects)
        n_fn = len(onto.functions)
        n_rel = len(onto.relations)
        ok = n_obj > 0 and n_fn > 0
        detail = f"objects={n_obj} functions={n_fn} relations={n_rel}"
        if not ok:
            detail += " —— 对象或指标为空，Ontology 可能未正确生成"
        return CheckResult("ontology", ok, detail)
    except Exception as e:  # noqa: BLE001
        return CheckResult("ontology", False, f"加载失败: {e}")


def _check_db(executor) -> CheckResult:
    # sqlite 介质：尝试只读打开
    if executor.dialect == "sqlite":
        try:
            conn = sqlite3.connect(f"file:{executor.dsn}?mode=ro", uri=True)
            cur = conn.execute("SELECT COUNT(*) FROM sqlite_master")
            n_tables = cur.fetchone()[0]
            conn.close()
            ok = n_tables > 0
            detail = f"SQLite 可读，schema 对象数={n_tables}"
            if not ok:
                detail += " —— 库为空，请运行 install.sh 重建样例库"
            return CheckResult("db", ok, detail)
        except sqlite3.Error as e:
            return CheckResult("db", False, f"SQLite 打开失败: {e}（请检查 DATA_AGENT_DB 或重建样例库）")
    # 远程方言：检查连接串已配置（真实连通性由首次调用验证）
    dsn = CONFIG.dsn_for(executor.dialect)
    if not dsn:
        return CheckResult("db", False,
                           f"未配置 DATA_AGENT_DSN_{executor.dialect.upper()} 连接串",
                           required=False)  # 允许未配置：只是该方言不可用
    return CheckResult("db", True, f"{executor.dialect} 连接串已配置")


def _check_log_dir() -> CheckResult:
    try:
        CONFIG.log_dir.mkdir(parents=True, exist_ok=True)
        probe = CONFIG.log_dir / ".write-probe"
        probe.write_text("ok")
        probe.unlink()
        return CheckResult("log_dir", True, f"日志目录可写: {CONFIG.log_dir}")
    except OSError as e:
        return CheckResult("log_dir", False, f"日志目录不可写: {e}")


def run_startup_checks(onto, executor) -> list[CheckResult]:
    """执行全部自检，返回结果列表。"""
    return [_check_ontology(onto), _check_db(executor), _check_log_dir()]


def startup_status(onto, executor) -> dict:
    """供 health_check 工具使用的状态快照（含自检结果）。"""
    checks = run_startup_checks(onto, executor)
    return {
        "ok": all(c.ok for c in checks if c.required),
        "dialect": executor.dialect,
        "ontology": {
            "objects": len(onto.objects),
            "functions": len(onto.functions),
            "relations": len(onto.relations),
            "families": len(onto.families),
        },
        "db": {"dsn": executor.dsn,
               "configured_remote": {k: bool(v) for k, v in CONFIG.remote_dsn.items()}},
        "checks": [{"name": c.name, "ok": c.ok, "required": c.required, "detail": c.detail}
                   for c in checks],
    }
