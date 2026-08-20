"""查询令牌（P0-1）：execute_sql 的执行授权。

把「翻译」与「执行」绑定：
  * semantic_translate 翻译成功 → 签发 query_token（绑定 SQL 原文 + 过期时间）
  * execute_sql 必须携带 query_token，且 SQL 必须与令牌绑定的一致，否则拒绝

安全属性：
  * 令牌绑定 SQL 原文（防篡改/换 SQL 重放）
  * 短 TTL（默认 300s）+ 惰性 GC，防长期有效
  * 令牌只在 server 进程内（内存），重启即失效，无落盘风险
  * 单令牌可多次执行（同一次翻译可重复取数，幂等只读查询）

实现：TTL/GC/容量/签发校验骨架复用 TokenStoreBase（与 confirm_token 一致）。
"""
from __future__ import annotations

import hashlib

from .token_base import TokenStoreBase


class QueryTokenStore(TokenStoreBase):
    """查询令牌：绑定 SQL 原文，校验须与签发时一致。"""

    def __init__(self, ttl_seconds: int = 300, max_tokens: int = 500):
        super().__init__(ttl_seconds=ttl_seconds, max_tokens=max_tokens)

    def _bind(self, entry, sql: str) -> None:
        entry.sql = sql

    def _matches(self, entry, sql: str) -> bool:
        return entry.sql == sql

    def _msg_invalid(self) -> str:
        return "query_token 无效或已过期，请先调用 semantic_translate 获取新的查询令牌"

    def _msg_expired(self) -> str:
        return "query_token 已过期，请重新调用 semantic_translate"

    def _msg_mismatch(self) -> str:
        return "SQL 与 query_token 不匹配：禁止绕过翻译引擎执行 SQL"

    # 供测试/审计：确定性指纹（不暴露内部 token）
    @staticmethod
    def fingerprint(sql: str) -> str:
        return hashlib.sha256(sql.encode("utf-8")).hexdigest()[:12]
