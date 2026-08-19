"""确认令牌（P4-14）：把「用户确认」从 prompt 约束升级为工具层强制。

问题：persona/skill 要求 L2+ 查询先 mql_explain 展示口径再请用户确认，
但工具层不强制——agent 可能跳过确认直接 semantic_translate。

方案：
  * mql_explain(mql) 成功 → 签发 confirm_token（绑定 MQL 指纹 + TTL）
  * semantic_translate(mql, confirm_token=...) 必须携带，且指纹必须匹配
    ——「确认过的查询」才能翻译执行，改了 MQL（指纹变化）就必须重新确认

与查询令牌（query_token）分工：
  * confirm_token —— 确认环节：MQL → 翻译（防跳过确认）
  * query_token   —— 执行环节：SQL → 执行（防绕过翻译）
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field


@dataclass
class _ConfirmEntry:
    fingerprint: str
    expires_at: float
    issued_at: float = field(default_factory=time.time)


class ConfirmTokenStore:
    def __init__(self, ttl_seconds: int = 600, max_tokens: int = 200):
        self.ttl_seconds = ttl_seconds
        self.max_tokens = max_tokens
        self._tokens: dict[str, _ConfirmEntry] = {}

    # ── 指纹：MQL 的确定性摘要（metrics/dimensions/filters/time_range）──
    @staticmethod
    def fingerprint(mql: dict) -> str:
        canonical = {
            "metrics": mql.get("metrics") or ([mql["metric"]] if mql.get("metric") else []),
            "dimensions": mql.get("dimensions", []),
            "filters": mql.get("filters", []),
            "time_range": mql.get("time_range"),
        }
        raw = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def issue(self, mql: dict) -> str:
        self._gc()
        token = secrets.token_hex(16)
        self._tokens[token] = _ConfirmEntry(
            fingerprint=self.fingerprint(mql),
            expires_at=time.time() + self.ttl_seconds)
        if len(self._tokens) > self.max_tokens:
            oldest = min(self._tokens, key=lambda t: self._tokens[t].issued_at)
            del self._tokens[oldest]
        return token

    def verify(self, token: str, mql: dict) -> tuple[bool, str]:
        """返回 (ok, 原因)。ok=True 表示令牌有效且与当前 MQL 指纹一致。"""
        self._gc()
        entry = self._tokens.get(token)
        if entry is None:
            return False, "confirm_token 无效或已过期，请先调用 mql_explain 获取确认令牌"
        if entry.expires_at < time.time():
            del self._tokens[token]
            return False, "confirm_token 已过期，请重新调用 mql_explain"
        if entry.fingerprint != self.fingerprint(mql):
            return False, "MQL 与 confirm_token 不匹配：查询已变更，需重新确认（mql_explain）"
        return True, ""

    def _gc(self) -> None:
        now = time.time()
        expired = [t for t, e in self._tokens.items() if e.expires_at < now]
        for t in expired:
            del self._tokens[t]

    def __len__(self) -> int:
        return len(self._tokens)
