"""运行日志（P2）：MCP server 的运行日志，jsonl + 轮转清理。

与审计日志（audit.jsonl）分工：
  * audit.jsonl —— 每笔工具调用的合规审计（谁/何时/查了什么/结果规模）
  * runtime.jsonl —— 运行态日志（启动/关闭/连接/异常/警告），面向排障

同一套轮转策略（单文件上限 + 保留份数），日志不无限增长。
"""
from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_logger: logging.Logger | None = None


def setup_runtime_logger(log_dir: Path, level: str = "INFO",
                         max_bytes: int = 10 * 1024 * 1024,
                         backup_count: int = 7) -> logging.Logger:
    """初始化运行日志（幂等）。返回 runtime logger。"""
    global _logger
    if _logger is not None:
        return _logger
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("data-agent.runtime")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    if not logger.handlers:
        handler = RotatingFileHandler(
            log_dir / "runtime.jsonl", maxBytes=max_bytes,
            backupCount=backup_count, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    _logger = logger
    return logger


def _emit(level: int, event: str, **fields: Any) -> None:
    if _logger is None:
        return
    rec = {
        "ts": __import__("time").strftime("%Y-%m-%dT%H:%M:%S%z"),
        "event": event,
    }
    rec.update(fields)
    try:
        _logger.log(level, json.dumps(rec, ensure_ascii=False, default=str))
    except Exception:
        pass


def log_info(event: str, **fields: Any) -> None:
    _emit(logging.INFO, event, **fields)


def log_warn(event: str, **fields: Any) -> None:
    _emit(logging.WARNING, event, **fields)


def log_error(event: str, **fields: Any) -> None:
    _emit(logging.ERROR, event, **fields)
