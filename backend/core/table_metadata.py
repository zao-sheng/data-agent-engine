"""表元数据采集器：从样例库/数仓提取每张表的元数据（供 metadata_search）。

输入：
  * SQLite 样例库（PRAGMA table_info 拿字段）
  * Ontology（对象名/层归属）
  * 血缘逻辑（seed.py 聚合语义 / LINEAGE 常量——metadata.py 亦引用同一份）

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


def _granularity_of(table: str, layer: str) -> str:
    if layer == "DIM":
        return "维度"
    if table.endswith("_1m"):
        return "月"
    if table.endswith("_1d"):
        return "日"
    return "明细_原子"


def _collect_one(db: Path | str, onto: Ontology, t: str,
                 entry: dict | None = None) -> dict:
    """采集单张表元数据（样例库有则 PRAGMA，无则从对象属性推导）。

    entry: 可选——注册中的对象 dict（新表未建时从中取 field_mapping+properties）。
    """
    # 表名解析（layer/domain/subject）复用 table_naming 单点规范
    from .table_naming import parse_table_name
    layer, domain, subject = parse_table_name(t)

    # 字段：样例库 PRAGMA 优先；表不存在时从对象属性推导
    fields = []
    try:
        conn = sqlite3.connect(db)
        cols = conn.execute(f"PRAGMA table_info({t})").fetchall()
        conn.close()
    except sqlite3.Error:
        cols = []
    if cols:
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
    else:
        # 表尚未建（路径 D 新表）：优先用注册 entry 的 field_mapping 推导
        obj = None
        fm = None
        if entry is not None:
            obj = entry
            for st in entry.get("source_tables", []):
                if st["table"] == t:
                    fm = st.get("field_mapping", {})
                    break
        else:
            owner = TABLE_OWNER.get(t)
            if owner and owner in onto.objects:
                obj = onto.objects[owner]
                for st in obj.get("source_tables", []):
                    if st["table"] == t:
                        fm = st.get("field_mapping", {})
                        break
        if fm and obj:
            for p in obj.get("properties", []):
                col = fm.get(p["name"], p["name"])
                fields.append({
                    "name": col,
                    "type": "TEXT",  # 未建表时类型未知，按业务属性 type 映射
                    "description": p.get("description", ""),
                    "is_pk": False,
                    "is_partition": col == "dt",
                })

    partition_col = "dt" if layer != "DIM" else ""
    # 业务域：entry（注册对象）显式 domain 优先，其次表名推断，兜底 ord
    obj_domain = (entry or {}).get("domain")
    return {
        "table_name": t,
        "layer": layer,
        "domain": obj_domain or domain or "ord",
        "subject": subject or "",
        "description": TABLE_DESC.get(t, ""),
        "granularity": _granularity_of(t, layer),
        "partition_col": partition_col,
        "owner_object": TABLE_OWNER.get(t),
        "fields": fields,
        "lineage": LINEAGE.get(t, {"source": "", "logic": "贴源/维表（无聚合加工）"}),
        "readiness": "T+1",
    }


def collect_table_metadata(db: Path | str, onto: Ontology) -> list[dict]:
    """从样例库 + Ontology 提取全部表元数据（样例库中存在的表）。"""
    conn = sqlite3.connect(db)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    conn.close()
    return [_collect_one(db, onto, t) for t in tables]


def collect_metadata_from_entry(db: Path | str, onto: Ontology,
                                obj_entry: dict) -> list[dict]:
    """按对象 entry 采集其 source_tables 的表元数据（路径 D 注册对象后自动联动）。

    直接基于注册的 entry（不依赖 Ontology 已含该对象），表已建采真实字段、
    未建从 entry 的 field_mapping 推导。
    """
    tables = [t["table"] for t in obj_entry.get("source_tables", [])]
    out = []
    for t in tables:
        meta = _collect_one(db, onto, t, entry=obj_entry)
        # 用 entry 补充归属对象与描述（新对象不在 TABLE_OWNER 时）
        if not meta["owner_object"]:
            meta["owner_object"] = obj_entry.get("name")
        if not meta["description"]:
            meta["description"] = obj_entry.get("description", "")
        out.append(meta)
    return out


def collect_metadata_for_object(db: Path | str, onto: Ontology,
                                obj_name: str) -> list[dict]:
    """按对象采集其 source_tables 的表元数据（路径 D 注册对象后自动联动）。

    表已建（样例库）→ PRAGMA 采真实字段；
    表未建（路径 D 新表）→ 从对象属性 + field_mapping 推导字段。
    """
    o = onto.get_object(obj_name)
    if not o:
        return []
    tables = [t["table"] for t in o.get("source_tables", [])]
    return [_collect_one(db, onto, t) for t in tables]


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
