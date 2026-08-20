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

实现：TTL/GC/容量/签发校验骨架复用 TokenStoreBase（与 query_token 一致）。
"""
from __future__ import annotations

import hashlib
import json

from .token_base import TokenStoreBase


class ConfirmTokenStore(TokenStoreBase):
    """确认令牌：绑定 MQL 指纹，MQL 变更即失效（需重新确认）。"""

    def __init__(self, ttl_seconds: int = 600, max_tokens: int = 200):
        super().__init__(ttl_seconds=ttl_seconds, max_tokens=max_tokens)

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

    def _bind(self, entry, mql: dict) -> None:
        entry.fingerprint = self.fingerprint(mql)

    def _matches(self, entry, mql: dict) -> bool:
        return entry.fingerprint == self.fingerprint(mql)

    def _msg_invalid(self) -> str:
        return "confirm_token 无效或已过期，请先调用 mql_explain 获取确认令牌"

    def _msg_expired(self) -> str:
        return "confirm_token 已过期，请重新调用 mql_explain"

    def _msg_mismatch(self) -> str:
        return "MQL 与 confirm_token 不匹配：查询已变更，需重新确认（mql_explain）"
