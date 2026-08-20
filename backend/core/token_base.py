"""令牌存储基类（共享）：TTL / 容量淘汰 / 惰性 GC / 签发校验骨架。

query_token（执行授权）与 confirm_token（确认授权）共用同一套生命周期
管理，仅差异在「条目绑定内容」与「指纹计算」——基类收敛通用逻辑，
子类实现 _fingerprint 与条目字段。

安全属性（两令牌一致）：
  * 令牌绑定内容（SQL 原文 / MQL 指纹），防篡改/换内容重放
  * 短 TTL + 惰性 GC，防长期有效
  * 进程内内存，重启即失效，无落盘
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field


@dataclass
class _TokenEntry:
    expires_at: float
    issued_at: float = field(default_factory=time.time)


class TokenStoreBase:
    """通用令牌存储：子类实现 _bind(entry, payload) 与 _matches(entry, payload)。"""

    entry_cls = _TokenEntry

    def __init__(self, ttl_seconds: int = 300, max_tokens: int = 500):
        self.ttl_seconds = ttl_seconds
        self.max_tokens = max_tokens
        self._tokens: dict[str, _TokenEntry] = {}

    # ── 子类实现点 ──────────────────────────────────────────
    def _bind(self, entry: _TokenEntry, payload) -> None:
        """把内容绑定到条目（如 SQL 原文 / MQL 指纹）。"""
        raise NotImplementedError

    def _matches(self, entry: _TokenEntry, payload) -> bool:
        """校验 payload 与条目绑定内容是否一致。"""
        raise NotImplementedError

    # ── 签发 ────────────────────────────────────────────────
    def issue(self, payload) -> str:
        self._gc()
        token = secrets.token_hex(16)
        entry = self.entry_cls(expires_at=time.time() + self.ttl_seconds)
        self._bind(entry, payload)
        self._tokens[token] = entry
        # 容量保护：超过上限时淘汰最旧令牌
        if len(self._tokens) > self.max_tokens:
            oldest = min(self._tokens, key=lambda t: self._tokens[t].issued_at)
            del self._tokens[oldest]
        return token

    # ── 校验 ────────────────────────────────────────────────
    def verify(self, token: str, payload) -> tuple[bool, str]:
        """返回 (ok, 原因)。ok=True 表示令牌有效且内容一致。"""
        self._gc()
        entry = self._tokens.get(token)
        if entry is None:
            return False, self._msg_invalid()
        if entry.expires_at < time.time():
            del self._tokens[token]
            return False, self._msg_expired()
        if not self._matches(entry, payload):
            return False, self._msg_mismatch()
        return True, ""

    # ── 错误文案（子类可定制）───────────────────────────────
    def _msg_invalid(self) -> str:
        return "令牌无效或已过期，请重新获取"

    def _msg_expired(self) -> str:
        return "令牌已过期，请重新获取"

    def _msg_mismatch(self) -> str:
        return "内容与令牌不匹配"

    # ── 内部 ────────────────────────────────────────────────
    def _gc(self) -> None:
        now = time.time()
        expired = [t for t, e in self._tokens.items() if e.expires_at < now]
        for t in expired:
            del self._tokens[t]

    def __len__(self) -> int:
        return len(self._tokens)
