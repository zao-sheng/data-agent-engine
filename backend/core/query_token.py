"""查询令牌（P0-1）：execute_sql 的执行授权。

背景：MCP 工具 `execute_sql` 若接受任意 SQL，调用方可绕过翻译引擎
（semantic_translate）直接查询物理表——行级权限、表选择、required_filter
全部形同虚设。查询令牌把「翻译」与「执行」绑定：
  * semantic_translate 翻译成功 → 签发 query_token（绑定 SQL 原文 + 过期时间）
  * execute_sql 必须携带 query_token，且 SQL 必须与令牌绑定的一致，否则拒绝

安全属性：
  * 令牌绑定 SQL 原文（防篡改/换 SQL 重放）
  * 短 TTL（默认 300s）+ 惰性 GC，防长期有效
  * 令牌只在 server 进程内（内存），重启即失效，无落盘风险
  * 单令牌可多次执行（同一次翻译可重复取数，幂等只读查询）
"""
from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass, field


@dataclass
class _TokenEntry:
    sql: str
    expires_at: float
    issued_at: float = field(default_factory=time.time)


class QueryTokenStore:
    def __init__(self, ttl_seconds: int = 300, max_tokens: int = 500):
        self.ttl_seconds = ttl_seconds
        self.max_tokens = max_tokens
        self._tokens: dict[str, _TokenEntry] = {}

    # ── 签发 ────────────────────────────────────────────────
    def issue(self, sql: str) -> str:
        self._gc()
        token = secrets.token_hex(16)
        self._tokens[token] = _TokenEntry(sql=sql,
                                          expires_at=time.time() + self.ttl_seconds)
        # 容量保护：超过上限时淘汰最旧令牌
        if len(self._tokens) > self.max_tokens:
            oldest = min(self._tokens, key=lambda t: self._tokens[t].issued_at)
            del self._tokens[oldest]
        return token

    # ── 校验 ────────────────────────────────────────────────
    def verify(self, token: str, sql: str) -> tuple[bool, str]:
        """返回 (ok, 原因)。ok=True 表示令牌有效且 SQL 与签发时一致。"""
        self._gc()
        entry = self._tokens.get(token)
        if entry is None:
            return False, "query_token 无效或已过期，请先调用 semantic_translate 获取新的查询令牌"
        if entry.expires_at < time.time():
            del self._tokens[token]
            return False, "query_token 已过期，请重新调用 semantic_translate"
        if entry.sql != sql:
            return False, "SQL 与 query_token 不匹配：禁止绕过翻译引擎执行 SQL"
        return True, ""

    # ── 内部 ────────────────────────────────────────────────
    def _gc(self) -> None:
        now = time.time()
        expired = [t for t, e in self._tokens.items() if e.expires_at < now]
        for t in expired:
            del self._tokens[t]

    def __len__(self) -> int:
        return len(self._tokens)

    # 供测试：确定性指纹（不暴露内部 token）
    @staticmethod
    def fingerprint(sql: str) -> str:
        return hashlib.sha256(sql.encode("utf-8")).hexdigest()[:12]
