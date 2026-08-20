"""数仓表名解析（共享）：{layer}_{domain}_{subject}_{粒度} 规范。

单点定义表名分解规则，供 ontology_loader（域推断）与 table_metadata
（表元数据采集）复用——消除两处近似实现导致的解析歧义。
"""
from __future__ import annotations

LAYER_PREFIXES = ("ODS", "DWD", "DWS", "ADS", "DIM")


def parse_table_name(table: str) -> tuple[str, str, str]:
    """按规范解析表名 → (layer, domain, subject)。

    dwd_ord_order_di → ("DWD", "ord", "order")
    dim_store        → ("DIM", "store", "")
    不规范名（无前缀/少于两段）→ 尽力取段，空段回 ""。
    """
    parts = table.split("_")
    if not parts:
        return "", "", ""
    layer = parts[0].upper() if parts[0].upper() in LAYER_PREFIXES else ""
    if layer:
        domain = parts[1] if len(parts) > 1 else ""
        subject = parts[2] if len(parts) > 2 else ""
    else:
        # 无规范前缀：整名视为 domain（如自定义表名）
        domain = parts[0]
        subject = parts[1] if len(parts) > 1 else ""
    return layer, domain, subject


def domain_of_table(table: str) -> str:
    """表名 → 业务域（无前缀表返回首段）。"""
    _layer, domain, _subject = parse_table_name(table)
    return domain
