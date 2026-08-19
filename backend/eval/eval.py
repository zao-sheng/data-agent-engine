"""评测门禁：Golden Dataset 回归。

对每条金标准用例：
  * 意图分类用例（expect_intent）：断言确定性意图识别结果（plan 层门禁）
  * MQL 用例：校验 → 翻译 → 执行，断言：
    - 应拒绝的用例必须被校验器拒绝
    - 其余用例：翻译成功、SQL 可执行、事实表 == 预期表、结果行数 > 0
通过率 ≥ 90% 才返回 0（CI 门禁），否则返回 1。

用法：python -m eval.eval [--threshold 0.9]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import Executor, MqlValidator, Ontology, Translator  # noqa: E402
from core.intent import classify_intent  # noqa: E402

BASE = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=0.9)
    ap.add_argument("--db", default=str(BASE / "seed" / "sample.db"))
    a = ap.parse_args()

    onto = Ontology(BASE / "ontology")
    validator = MqlValidator(onto)
    translator = Translator(onto)
    executor = Executor(a.db)

    cases = [json.loads(line) for line in
             (BASE / "eval" / "golden_dataset.jsonl").read_text().splitlines() if line.strip()]
    passed, failed = 0, []

    for i, case in enumerate(cases, 1):
        # 意图分类用例（plan 层门禁）：断言确定性意图识别结果
        if case.get("expect_intent"):
            try:
                r = classify_intent(onto, case["question"])
                if r["intent"] != case["expect_intent"]:
                    raise AssertionError(
                        f"意图识别错误: 期望 {case['expect_intent']}，实际 {r['intent']}"
                        f"（evidence={r['evidence'][:4]}）")
                if case.get("expect_mixed") and case["expect_mixed"] not in \
                        [m["intent"] for m in r.get("mixed", [])]:
                    raise AssertionError(
                        f"混合意图缺失: 期望含 {case['expect_mixed']}，实际 {r.get('mixed')}")
                if case.get("expect_metrics_missing") is not None and \
                        r["metrics_missing"] != case["expect_metrics_missing"]:
                    raise AssertionError(
                        f"metrics_missing 不符: 期望 {case['expect_metrics_missing']}，"
                        f"实际 {r['metrics_missing']}")
                passed += 1
            except AssertionError as e:
                failed.append((i, case["question"], str(e)))
            continue

        mql = dict(case["mql"])
        user = mql.pop("_user", None)
        try:
            v = validator.validate(mql)
            if case.get("expect_reject"):
                if v["ok"]:
                    raise AssertionError("应被拒绝的用例通过了校验")
                passed += 1
                continue
            if not v["ok"]:
                raise AssertionError(f"校验失败: {v['errors']}")
            r = translator.translate(mql, user or {}, dialect="sqlite")
            if "error" in r:
                raise AssertionError(f"翻译失败: {r['error']}")
            if case.get("expect_multi"):
                if not r["metadata"].get("multi_table"):
                    raise AssertionError("期望多表合并（multi_table）")
                got = set(r["metadata"].get("metrics", []))
                if not got >= set(case.get("expect_metrics", [])):
                    raise AssertionError(f"指标缺失: 期望 {case['expect_metrics']}，实际 {sorted(got)}")
            elif case.get("expect_table") and r["metadata"]["fact_table"] != case["expect_table"]:
                raise AssertionError(f"表选择错误: 期望 {case['expect_table']}，实际 {r['metadata']['fact_table']}")
            res = executor.execute(r["sql"])
            if "error" in res:
                raise AssertionError(f"SQL 执行失败: {res['error']}")
            if case.get("expect_rows_eq") is not None:
                if res["row_count"] != case["expect_rows_eq"]:
                    raise AssertionError(f"结果行数 {res['row_count']} ≠ 期望 {case['expect_rows_eq']}")
            elif res["row_count"] <= case.get("expect_rows_gt", 0):
                raise AssertionError(f"结果为空（期望 > {case.get('expect_rows_gt', 0)} 行）")
            # 多指标断言：结果列必须包含全部指标名
            for m in case.get("expect_metrics", []):
                if m not in res.get("columns", []):
                    raise AssertionError(f"结果缺少指标列 {m}")
            passed += 1
        except AssertionError as e:
            failed.append((i, case["question"], str(e)))

    rate = passed / len(cases)
    print(f"评测门禁：{passed}/{len(cases)} 通过（{rate:.0%}）")
    for i, q, err in failed:
        print(f"  ✗ #{i} [{q}] {err}")
    ok = rate >= a.threshold and not failed
    print("✅ 通过" if ok else f"❌ 未达门禁（阈值 {a.threshold:.0%}）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
