"""运行时配置：环境变量 + backend/.env（可选，P4 完整外置）。

优先级：进程环境变量 > backend/.env > 默认值。
所有 data-agent 配置项统一 `DATA_AGENT_` 前缀，避免与 DSH/宿主环境冲突。
"""
from __future__ import annotations

import os
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """极简 .env 解析（不引第三方依赖）：KEY=VALUE，支持 # 注释与引号。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            os.environ.setdefault(key, value)


_load_dotenv(BACKEND_ROOT / ".env")


class Config:
    """集中读取配置。属性即配置项，新增配置在此声明并给默认值。"""

    def __init__(self) -> None:
        # 本体存储（1A/1B/2）：yaml（默认，发布基线快照）/ sqlite（编译产物）/ supabase（多人编辑真源）
        self.ontology_dir: str = os.environ.get(
            "DATA_AGENT_ONTOLOGY", str(BACKEND_ROOT / "ontology"))
        self.ontology_store: str = os.environ.get("DATA_AGENT_ONTOLOGY_STORE", "yaml").lower()
        self.ontology_db: str = os.environ.get(
            "DATA_AGENT_ONTOLOGY_DB", str(BACKEND_ROOT / "ontology.db"))
        # Supabase（阶段 2）：本体真源，多人编辑
        self.supabase_url: str = os.environ.get("DATA_AGENT_SUPABASE_URL", "")
        self.supabase_key: str = os.environ.get("DATA_AGENT_SUPABASE_KEY", "")
        self.supabase_schema: str = os.environ.get("DATA_AGENT_SUPABASE_SCHEMA", "public")
        self.db: str = os.environ.get(
            "DATA_AGENT_DB", str(BACKEND_ROOT / "seed" / "sample.db"))
        self.log_dir: Path = Path(os.environ.get(
            "DATA_AGENT_LOG_DIR", str(BACKEND_ROOT / "logs")))
        self.log_level: str = os.environ.get("DATA_AGENT_LOG_LEVEL", "INFO").upper()
        # 查询令牌（P0-1）：execute_sql 必须携带 semantic_translate 签发的令牌
        self.query_token_ttl: int = int(os.environ.get("DATA_AGENT_TOKEN_TTL", "300"))
        self.query_token_max: int = int(os.environ.get("DATA_AGENT_TOKEN_MAX", "500"))
        # 确认令牌（P4-14）：semantic_translate 必须携带 mql_explain 签发的令牌
        self.confirm_token_ttl: int = int(os.environ.get("DATA_AGENT_CONFIRM_TOKEN_TTL", "600"))
        self.confirm_token_max: int = int(os.environ.get("DATA_AGENT_CONFIRM_TOKEN_MAX", "200"))
        # 审计日志（P0-3）：轮转参数——单文件上限与保留份数
        self.audit_max_bytes: int = int(os.environ.get("DATA_AGENT_AUDIT_MAX_BYTES",
                                                       str(10 * 1024 * 1024)))
        self.audit_backup_count: int = int(os.environ.get("DATA_AGENT_AUDIT_BACKUPS", "7"))
        # 默认方言（P1-5）：sqlite 已实现；mysql/doris/hive/sparksql 走远程驱动
        self.dialect_default: str = os.environ.get("DATA_AGENT_DIALECT", "sqlite")
        # 远程连接超时（秒）
        self.remote_timeout_s: int = int(os.environ.get("DATA_AGENT_REMOTE_TIMEOUT", "15"))
        # 远程数仓连接串：DATA_AGENT_DSN_MYSQL / DATA_AGENT_DSN_DORIS / ...
        # 格式：scheme://user:pass@host:port/db（scheme = mysql|doris|hive|sparksql）
        self.remote_dsn: dict[str, str] = {
            k.removeprefix("DATA_AGENT_DSN_").lower(): v
            for k, v in os.environ.items()
            if k.startswith("DATA_AGENT_DSN_") and v.strip()
        }

    # ── 便捷访问 ────────────────────────────────────────────
    def dsn_for(self, dialect: str) -> str | None:
        return self.remote_dsn.get(dialect.lower())

    def has_remote_dsn(self, dialect: str) -> bool:
        return dialect.lower() in self.remote_dsn


# 模块级单例（server / executor 共用同一份配置）
CONFIG = Config()
