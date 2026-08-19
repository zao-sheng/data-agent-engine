"""表元数据采集器：从样例库/数仓提取每张表的元数据（供 metadata_search）。

输入：
  * SQLite 样例库（PRAGMA table_info 拿字段）
  * Ontology（对象名/层归属）
  * 血缘逻辑（seed.py 聚合语义 / metadata._LINEAGE）

输出：15 张表的完整元数据 [{table_name, layer, domain, subject, description,
      granularity, partition_col, owner_object, fields[], lineage{}, readiness}]

用途：
  1. ontology_tables 批量入库（Supabase 真源）——metadata_search 查库
  2. 或生成 JSON/YAML 基线快照

真实数仓场景：换真实库后，本采集器从真实元数据接口替换输入即可，
输出结构不变（与 ontology_tables 表结构对应）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .ontology_loader import Ontology

# 血缘逻辑（与 metadata._LINEAGE 一致，作为入库来源）
LINEAGE: dict[str, dict] = {
    "dws_ord_order_1d": {"source": "dwd_ord_order_di",
                         "logic": "按 dt/region_id/store_id 聚合：订单量、有效订单量、下单金额、下单用户数"},
    "dws_ord_pay_1d": {"source": "dwd_ord_pay_di",
                       "logic": "按 dt/region_id/store_id 聚合：支付笔数、支付金额、支付用户数（过滤 is_valid=1）"},
    "dws_ord_consume_1d": {"source": "dwd_ord_consume_di",
                           "logic": "按 dt/region_id/store_id 聚合：消费笔数、消费金额、消费用户数（过滤 is_valid=1）"},
    "dws_ord_refund_1d": {"source": "dwd_ord_refund_di",
                          "logic": "按 dt/region_id/store_id 聚合：退款笔数、退款金额（过滤 is_valid=1）"},
    "ads_ord_gmv_1d": {"source": "dwd_ord_pay_di",
                       "logic": "按 dt 聚合：GMV（SUM(pay_amt)，过滤 is_valid=1）"},
    "ads_ord_gmv_1m": {"source": "dwd_ord_pay_di",
                       "logic": "按月聚合：GMV、支付笔数（过滤 is_valid=1）"},
    "ads_ord_user_1d": {"source": "dwd_ord_pay_di",
                        "logic": "按 dt 聚合：活跃用户数（样例置 0）、下单用户数、新用户数（样例置 0）"},
}

# 表 → 归属对象（Ontology 对象名）
TABLE_OWNER: dict[str, str] = {
    "dwd_ord_order_di": "Order", "dws_ord_order_1d": "Order",
    "dwd_ord_pay_di": "Payment", "dws_ord_pay_1d": "Payment",
    "dwd_ord_consume_di": "Consume", "dws_ord_consume_1d": "Consume",
    "dwd_ord_refund_di": "Refund", "dws_ord_refund_1d": "Refund",
    "ads_ord_gmv_1d": "Payment", "ads_ord_gmv_1m": "Payment",
    "ads_ord_user_1d": "Payment",
    "dim_product": "Product", "dim_store": "Store",
    "dim_region": "Region", "dim_user": "ActiveUser",
}

# 表说明（业务描述）
TABLE_DESC: dict[str, str] = {
    "dwd_ord_order_di": "下单明细：每行一笔有效/取消订单，含金额与状态",
    "dwd_ord_pay_di": "支付明细：每行一笔有效支付，GMV 口径来源",
    "dwd_ord_consume_di": "消费明细：到店消费/核销记录",
    "dwd_ord_refund_di": "退款明细：有效退款记录",
    "dws_ord_order_1d": "订单日汇总：按日+区域+门店聚合下单指标",
    "dws_ord_pay_1d": "支付日汇总：按日+区域+门店聚合支付指标",
    "dws_ord_consume_1d": "消费日汇总：按日+区域+门店聚合消费指标",
    "dws_ord_refund_1d": "退款日汇总：按日+区域+门店聚合退款指标",
    "ads_ord_gmv_1d": "GMV 应用层日汇总：全站 GMV/订单/支付笔数",
    "ads_ord_gmv_1m": "GMV 应用层月汇总：按月聚合 GMV",
    "ads_ord_user_1d": "用户应用层日汇总：活跃/下单/新用户数",
    "dim_product": "产品维度表：产品分类与品类",
    "dim_store": "门店维度表：门店类型与城市归属",
    "dim_region": "区域维度表：区域-省份-城市层级",
    "dim_user": "用户维度表：用户状态与等级",
}


def _layer_of(table: str) -> str:
    return table.split("_")[0].upper()


def _granularity_of(table: str, layer: str) -> str:
    if layer == "DIM":
        return "维度"
    if table.endswith("_1m"):
        return "月"
    if table.endswith("_1d"):
        return "日"
    return "明细_原子"


def collect_table_metadata(db: Path | str, onto: Ontology) -> list[dict]:
    """从样例库 + Ontology 提取 15 张表元数据。"""
    conn = sqlite3.connect(db)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    conn.close()

    out: list[dict] = []
    for t in tables:
        layer = _layer_of(t)
        parts = t.split("_")
        domain = parts[1] if len(parts) > 1 and layer != "DIM" else (
            parts[1] if len(parts) > 1 else "")
        subject = parts[2] if len(parts) > 2 else (parts[1] if len(parts) > 1 else "")
        conn = sqlite3.connect(db)
        pk_cols = {r[5] for r in conn.execute(f"PRAGMA table_info({t})") if r[5] > 0}
        cols = conn.execute(f"PRAGMA table_info({t})").fetchall()
        conn.close()
        fields = []
        for cid, name, ctype, notnull, dflt, pk in cols:
            fields.append({
                "name": name,
                "type": "TEXT" if ctype.upper() in ("TEXT",) else
                        ("INTEGER" if "INT" in ctype.upper() else
                         ("REAL" if "REAL" in ctype.upper() else ctype.upper())),
                "description": "",
                "is_pk": bool(pk),
                "is_partition": name == "dt",
            })
        partition_col = "dt" if layer != "DIM" else ""
        out.append({
            "table_name": t,
            "layer": layer,
            "domain": domain or "ord",
            "subject": subject or "",
            "description": TABLE_DESC.get(t, ""),
            "granularity": _granularity_of(t, layer),
            "partition_col": partition_col,
            "owner_object": TABLE_OWNER.get(t),
            "fields": fields,
            "lineage": LINEAGE.get(t, {"source": "", "logic": "贴源/维表（无聚合加工）"}),
            "readiness": "T+1",
        })
    return out


def main() -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(description="采集表元数据（样例库 → JSON）")
    ap.add_argument("--db", default=str(Path(__file__).resolve().parent / "seed" / "sample.db"))
    ap.add_argument("--ontology", default=str(Path(__file__).resolve().parent / "ontology"))
    ap.add_argument("--out", default=None, help="输出 JSON 路径（默认打印到 stdout）")
    a = ap.parse_args()

    onto = Ontology(a.ontology)
    metas = collect_table_metadata(a.db, onto)
    text = json.dumps(metas, ensure_ascii=False, indent=1)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"✅ 已采集 {len(metas)} 张表元数据 → {a.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
