"""审计日志（P0-3）：谁、何时、查了什么、结果如何。

每条工具调用落一行 JSON（jsonl），字段稳定、可被审计系统消费：
  ts / tool / action / args 摘要 / outcome / elapsed_ms / 关键业务字段。

安全与运维约束：
  * 记录「查询意图」与「结果规模」，不记录敏感原始数据本身（SQL 原文可配）
  * 轮转清理（RotatingFileHandler）：单文件上限 + 保留份数，日志不无限增长
  * 写失败静默降级（打 stderr），绝不影响取数主链路
"""
from __future__ import annotations

import json
import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_audit: logging.Logger | None = None


def setup_audit_logger(log_dir: Path, max_bytes: int = 10 * 1024 * 1024,
                       backup_count: int = 7) -> logging.Logger:
    """初始化审计日志（幂等）。返回审计 logger。"""
    global _audit
    if _audit is not None:
        return _audit
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("data-agent.audit")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = RotatingFileHandler(
            log_dir / "audit.jsonl", maxBytes=max_bytes,
            backupCount=backup_count, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    _audit = logger
    return logger


def audit(tool: str, action: str, outcome: str, *, elapsed_ms: int,
          args: dict[str, Any] | None = None, **extra: Any) -> None:
    """写一条审计记录（线程安全，失败静默）。"""
    if _audit is None:
        return
    rec: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "tool": tool,
        "action": action,
        "outcome": outcome,
        "elapsed_ms": elapsed_ms,
    }
    if args:
        rec["args"] = args
    rec.update(extra)
    try:
        _audit.info(json.dumps(rec, ensure_ascii=False, default=str))
    except Exception:
        # 审计写入失败绝不阻塞主流程
        print(f"[audit-drop] {tool}/{action}: {outcome}", file=__import__("sys").stderr)
