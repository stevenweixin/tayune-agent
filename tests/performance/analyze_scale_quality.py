"""汇总模拟规模测试与真实逐题复核，严格区分拒绝、协议成功和业务完成。"""
import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from app.core.runtime import ROOT

VERDICTS = {"meets", "partial", "failed", "clarification"}


def quality_summary(rows, reviews):
    indexed = {(item["profile"], item["id"]): item for item in reviews}
    if len(indexed) != len(reviews) or set(indexed) != {(row["profile"], row["id"]) for row in rows}:
        raise ValueError("Review coverage must match every observed profile/case exactly once")
    if any(item["verdict"] not in VERDICTS for item in reviews):
        raise ValueError("Unknown review verdict")
    profiles = {}
    for profile in sorted({row["profile"] for row in rows}):
        selected = [row for row in rows if row["profile"] == profile]
        positive = [row for row in selected if row["category"] not in {"restricted", "out_of_scope"}]
        meets = [row for row in positive if indexed[(profile, row["id"])]["verdict"] == "meets"]
        effective = [row for row in meets if row["http_status"] == 200 and not row["result"].get("degraded")
                     and row["within_deadline"]]
        profiles[profile] = {"submitted": len(selected),
            "verdicts": dict(Counter(indexed[(profile, row["id"])]["verdict"] for row in selected)),
            "positive_submitted": len(positive), "positive_meets": len(meets),
            "positive_effective_within15s": len(effective),
            "positive_effective_denominator": len(positive), "independent_answer_accuracy": None}
    return profiles


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    root = args.input.resolve()
    if not root.is_relative_to(ROOT / "reports"):
        parser.error("Reports directory required")
    rows = [json.loads(line) for line in (root / "quality/answers.jsonl").read_text().splitlines()]
    review = json.loads((root / "quality_review.json").read_text())
    summary = quality_summary(rows, review["cases"])
    manifest = json.loads((root / "quality/manifest.json").read_text())
    changed = [name for name, digest in manifest["code_sha256"].items()
               if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest]
    attempts = [json.loads(line) for line in (root / "quality/model_attempts.jsonl").read_text().splitlines()]
    task_ids = {row["result"].get("task_id") for row in rows}
    audit = {"changed_frozen_files": changed, "physical_attempts": len(attempts),
             "linked_attempts": sum(row.get("run_id") in task_ids for row in attempts),
             "known_usage": all(row["usage_known"] for row in attempts),
             "estimated_usd": sum(row["estimated_usd"] for row in attempts),
             "roles": dict(Counter(row["role"] for row in attempts))}
    lines = ["# 压力前后24题逐项复核", "", "助手按冻结判据复核；非独立人工金标。并发差异不直接归因于负载。", "",
             "| 负载 | 编号 | 问题 | 判定 | 观察 |", "| --- | --- | --- | --- | --- |"]
    indexed = {(item["profile"], item["id"]): item for item in review["cases"]}
    labels = {"meets": "满足判据", "partial": "部分完成", "failed": "未完成", "clarification": "合理澄清"}
    for row in sorted(rows, key=lambda item: (item["profile"], item["id"])):
        item = indexed[(row["profile"], row["id"])]
        cells = [row["profile"], row["id"], row["question"], labels[item["verdict"]], item["note"]]
        lines.append("| " + " | ".join(cell.replace("|", "\\|").replace("\n", " ") for cell in cells) + " |")
    (root / "quality_review.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = {"profiles": summary, "audit": audit, "review_kind": review["review_kind"]}
    (root / "quality_analysis.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if changed:
        raise ValueError("Source changed since paid freeze")


if __name__ == "__main__":
    main()
