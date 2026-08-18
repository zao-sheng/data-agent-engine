"""本体半自动构建器：从数据库表结构生成 Ontology YAML 骨架。

自动推断：
  * 表分类：dwd_/dws_/ads_ → 事实表；dim_ → 维度表
  * 字段映射：业务属性名归一（去掉 _amt/_cnt 后缀的物理味，保留可读名）
  * 必要过滤：is_valid / valid / is_deleted → required_filters
  * 关系候选：*_id 字段 → 若存在同名 dim_ 表则生成 Relation（join_key）
  * 粒度：表名含 1d/1m → day/month；明细表（_di）→ 全粒度

人工补全（自动生成的骨架无法推断）：
  * 对象 description（LLM 直接读取，必须人工写清业务含义）
  * 指标 formula / do_not / 版本（口径，必须人工确认）
  * 关系 join_key 核对（跨表命名不一致时人工修正）

用法：
  python -m builder.build_ontology --sqlite seed/sample.db --out ontology
  python -m builder.build_ontology --connect "mysql+pymysql://..." --out ontology   # 真实库（需自实现元数据读取）
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from pathlib import Path

import yaml

DIM_PREFIX = re.compile(r"^dim_(.+)$")
FACT_PREFIX = re.compile(r"^(dwd|dws|ads)_(.+?)(?:_\d+[dm])?$")
REQUIRED_FILTER_COLS = {"is_valid", "valid", "is_deleted"}
GRAN_BY_SUFFIX = {"1d": ["day"], "1m": ["month"], "_di": ["day", "week", "month", "quarter", "year"]}
FULL_GRAN = ["day", "week", "month", "quarter", "year"]


def _norm_prop(col: str) -> str:
    """物理列 → 业务属性名（尽力归一，人工可改）"""
    return col


def infer_granularity(table: str) -> list[str]:
    for suffix, gs in GRAN_BY_SUFFIX.items():
        if table.endswith(suffix):
            return gs
    return ["day"]


def build(sqlite_path: Path) -> dict:
    conn = sqlite3.connect(sqlite_path)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    objects, relations = [], []

    for t in sorted(tables):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
        m_dim = DIM_PREFIX.match(t)
        if m_dim:
            obj_name = m_dim.group(1).title()
            props = [{"name": _norm_prop(c), "type": "string"} for c in cols if c not in ("dt",)]
            objects.append({
                "name": obj_name, "display_name": obj_name, "description": "",  # 🔴 人工补
                "properties": props,
                "source_tables": [{"table": t, "layer": "DIM", "authority": "gold",
                                   "joinable": False, "granularities": [],
                                   "available_dims": [_norm_prop(c) for c in cols if c not in ("dt",)],
                                   "field_mapping": {_norm_prop(c): c for c in cols if c not in ("dt",)}}],
            })
            continue

        m_fact = FACT_PREFIX.match(t)
        if not m_fact:
            continue
        obj_name = m_fact.group(2).split("_")[0].title() or "Fact"
        required = [f"{c} = 1" for c in cols if c in REQUIRED_FILTER_COLS]
        id_cols = [c for c in cols if c.endswith("_id") and c not in ("order_id",)]
        field_mapping = {_norm_prop(c): c for c in cols if c not in ("dt",)}
        # *_id 与维度表同源 → 关系候选
        for c in id_cols:
            dim_table = f"dim_{c[:-3]}"
            if dim_table in tables:
                relations.append({"source": obj_name, "target": c[:-3].title(),
                                  "type": "belongs_to", "join_key": c, "cardinality": "N:1"})
        objects.append({
            "name": obj_name, "display_name": obj_name, "description": "",  # 🔴 人工补
            "required_filters": required,
            "properties": [{"name": _norm_prop(c), "type": "string"} for c in cols if c not in ("dt",)],
            "source_tables": [{"table": t, "layer": m_fact.group(1).upper(), "authority": "gold",
                               "joinable": m_fact.group(1) == "dwd",
                               "granularities": infer_granularity(t),
                               "available_dims": [],
                               "field_mapping": field_mapping}],
        })
    conn.close()
    return {"objects": objects, "relations": relations}


def dump(base: Path, data: dict) -> None:
    base.mkdir(parents=True, exist_ok=True)
    (base / "objects.yaml").write_text(
        "# 自动生成骨架 —— 人工补：description / required_filters 核对 / granularities 核对\n"
        + yaml.safe_dump({"objects": data["objects"]}, allow_unicode=True, sort_keys=False))
    (base / "relations.yaml").write_text(
        "# 自动生成骨架 —— 人工核对 join_key（跨表命名不一致时修正）\n"
        + yaml.safe_dump({"relations": data["relations"]}, allow_unicode=True, sort_keys=False))
    # functions.yaml 生成空模板
    (base / "functions.yaml").write_text(
        "# 自动生成骨架 —— 🔴 人工填写：指标口径（formula/required_filters/版本）\n"
        "functions:\n  - name: gmv\n    display_name: GMV\n    description: \"\"\n"
        "    owner: Payment\n    formula: \"\"\n    required_filters: []\n"
        "    supported_dimensions: []\n    supported_granularities: [day, week, month, quarter, year]\n"
        "    do_not: \"\"\n    version: v1.0\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sqlite", default=None, help="SQLite 库路径（样例模式）")
    ap.add_argument("--connect", default=None, help="真实库连接串（需自实现元数据读取）")
    ap.add_argument("--out", default="ontology")
    a = ap.parse_args()
    if a.connect:
        raise SystemExit("真实库模式：请把 PRAGMA 读取替换为你的元数据 API/驱动，再跑本脚本")
    if not a.sqlite:
        raise SystemExit("请指定 --sqlite 样例库 或 --connect 真实库")
    data = build(Path(a.sqlite))
    dump(Path(a.out), data)
    print(f"✅ 本体骨架已生成到 {a.out}/")
    print("   🔴 接下来人工补：description、指标 formula/口径、relations join_key 核对")


if __name__ == "__main__":
    main()
