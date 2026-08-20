"""建模方案生成器（路径 D 阶段 2/3 合并）：变更清单 → 配对 DDL+ETL。

背景问题：阶段 2 调 ddl_generate、阶段 3 调 etl_generate，两者分离且单表粒度，
agent 可能生成 2 个 DDL 却只生成 1 个 ETL（数量不对应）。本模块把「变更清单」
作为单一输入，一次产出**配对的 {DDL, ETL} 列表**，工具层强制：
  * 变更类型 = 完全新增 / 新增字段 → 必须同时有 DDL 与 ETL（成对）
  * 变更类型 = 修改逻辑 → 只有 ETL，无 DDL
  * 变更类型 = 本体注册 → 无 DDL/ETL（仅注册项）
  * 数量校验：新增类变更数 == DDL 数 == ETL 数，不一致即报错

可编辑性：每个变更带 `editable` 字段列出可修改项（obj_name/layer/domain/
subject/metrics/dimensions），供确认环节逐项编辑。
"""
from __future__ import annotations

from .ddl_gen import generate_ddl
from .etl_gen import generate_etl
from .ontology_loader import Ontology

VALID_TYPES = {"create", "add_field", "modify_logic", "register"}


def _validate_change(c: dict) -> str | None:
    """校验单个变更项，返回错误信息（无则 None）。"""
    ctype = c.get("type")
    if ctype not in VALID_TYPES:
        return f"变更类型 {ctype!r} 非法，应为 {sorted(VALID_TYPES)}"
    if not c.get("obj_name"):
        return "变更缺少 obj_name（目标 Ontology 对象）"
    if ctype in ("create", "add_field") and not c.get("layer"):
        return f"变更 {c.get('obj_name')} 缺少 layer（DWD/DWS/ADS/DIM）"
    return None


def generate_modeling_plan(onto: Ontology, changes: list[dict]) -> dict:
    """变更清单 → 配对 DDL+ETL 列表。

    changes: [{type, obj_name, layer?, domain?, subject?, metrics?, dimensions?,
               field_changes?（add_field 时的字段说明）}]
    返回 {changes: [逐项 {变更信息 + ddl/etl 配对 + editable}], summary}
    """
    if not changes:
        return {"error": "变更清单为空，无法生成建模方案"}

    out_changes = []
    ddl_count = etl_count = create_count = create_etl_count = 0
    errors = []

    for i, c in enumerate(changes, 1):
        err = _validate_change(c)
        if err:
            errors.append(f"变更 #{i}: {err}")
            continue
        ctype = c["type"]
        obj = c["obj_name"]
        layer = c.get("layer")
        # domain 缺省取对象自身业务域（本体治理字段），跨域新建可显式传 domain
        obj_meta = onto.get_object(obj) or {}
        domain = c.get("domain") or obj_meta.get("domain") or "ord"
        subject = c.get("subject")
        metrics = c.get("metrics")
        dimensions = c.get("dimensions")

        entry: dict = {
            "index": i,
            "type": ctype,
            "obj_name": obj,
            "layer": layer,
            "domain": domain,
            "subject": subject,
            "metrics": metrics,
            "dimensions": dimensions,
            "editable": ["obj_name", "layer", "domain", "subject", "metrics", "dimensions"],
        }

        if ctype in ("create", "add_field"):
            # 完全新增 / 新增字段
            create_count += 1
            ddl = generate_ddl(onto, obj, layer, domain=domain, subject=subject)
            if "error" in ddl:
                errors.append(f"变更 #{i} DDL 失败: {ddl['error']}")
                continue
            ddl_count += 1
            entry["ddl"] = ddl
            # DIM 维表无聚合 ETL；事实表（DWD/DWS/ADS）必须有 ETL 成对
            if layer == "DIM":
                create_etl_count += 1  # 视为配对成立（无需 ETL）
                entry["pair"] = {"ddl_table": ddl["table"], "etl_target": None,
                                 "etl_source": None,
                                 "note": "DIM 维表无聚合 ETL（维表由源系统/手动维护）"}
            else:
                etl = generate_etl(onto, obj, layer, domain=domain, subject=subject,
                                   metrics=metrics, dimensions=dimensions)
                if "error" in etl:
                    # ETL 失败（如无 DWD 明细源）→ 明确提示该变更无法生成 ETL
                    errors.append(f"变更 #{i} ETL 失败: {etl['error']}")
                    continue
                etl_count += 1
                create_etl_count += 1
                entry["etl"] = etl
                entry["pair"] = {"ddl_table": ddl["table"], "etl_target": etl["target"],
                                 "etl_source": etl["source"]}
        elif ctype == "modify_logic":
            # 修改逻辑：只 ETL（无 DDL）
            etl = generate_etl(onto, obj, layer, domain=domain, subject=subject,
                               metrics=metrics, dimensions=dimensions)
            if "error" in etl:
                errors.append(f"变更 #{i} ETL 失败: {etl['error']}")
                continue
            etl_count += 1
            entry["etl"] = etl
            entry["pair"] = {"ddl_table": None, "etl_target": etl["target"],
                             "etl_source": etl["source"]}
        elif ctype == "register":
            # 本体注册：无 DDL/ETL
            entry["pair"] = {"ddl_table": None, "etl_target": None,
                             "etl_source": None}
            entry["note"] = "本体注册：编辑 backend/ontology/ 注册对象/指标/属性/关系"

        out_changes.append(entry)

    # 数量一致性校验：事实表新增（create/add_field 非 DIM）必须 DDL 与 ETL 成对；
    # DIM 维表 create 无需 ETL（create_etl_count 已计入配对成立）
    if create_count != ddl_count or create_count != create_etl_count:
        errors.append(
            f"配对数量不一致: 新增类变更 {create_count} 个，DDL {ddl_count} 个，"
            f"新增类 ETL {create_etl_count} 个——每个新增/加字段事实表必须同时有 DDL 与 ETL"
            f"（DIM 维表无需 ETL）")

    summary = {
        "total_changes": len(changes),
        "generated": len(out_changes),
        "create": create_count,
        "ddl": ddl_count,
        "etl": etl_count,
        "paired": create_count == ddl_count == create_etl_count and not errors,
        "editable_fields": ["obj_name", "layer", "domain", "subject", "metrics",
                            "dimensions", "type"],
    }
    return {"changes": out_changes, "summary": summary, "errors": errors,
            "review_required": True}
