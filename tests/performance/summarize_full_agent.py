"""只读取既有评测文件并汇总；不发起API请求，也不把代理契约当语义准确率。"""
import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from tests.performance.run_offline import distribution

ROOT = Path(__file__).resolve().parents[2]
REVIEW_LABELS = {"meets_rubric", "partial", "not_completed", "appropriate_clarification"}


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def review_counts(rows, review):
    """逐题复核必须完整且无重复，防止只挑好答案或静默漏题。"""
    ids = [row["id"] for row in rows]
    reviewed = [row["id"] for row in review["cases"]]
    if len(set(reviewed)) != len(reviewed) or set(ids) != set(reviewed):
        raise ValueError("Review must cover every functional case exactly once")
    if any(row["verdict"] not in REVIEW_LABELS for row in review["cases"]):
        raise ValueError("Unknown review verdict")
    return dict(Counter(row["verdict"] for row in review["cases"]))


def analyze(base):
    agent = base / "agent"
    rows = read_jsonl(agent / "functional.jsonl")
    bursts = read_jsonl(agent / "bursts.jsonl")
    attempts = read_jsonl(agent / "model_attempts.jsonl")
    review = json.loads((base / "assistant_review.json").read_text(encoding="utf-8"))
    manifest = json.loads((agent / "manifest.json").read_text(encoding="utf-8"))
    tasks = {row["result"]["task_id"] for row in rows + bursts if row["result"].get("task_id")}
    linked_attempts = [row for row in attempts if row.get("run_id") in tasks]
    changed = []
    for name, expected in manifest["code_sha256"].items():
        path = ROOT / name
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            changed.append(name)
    classification = []
    for category in ("product", "knowledge", "restricted", "out_of_scope"):
        expected = "in_scope" if category in ("product", "knowledge") else category
        events = [event for row in rows if row["category"] == category
                  for event in row["audit"] if event["kind"] == "classification"]
        classification.append({"category": category, "n": len(events),
                               "matches_case_label": sum(event["label"] == expected for event in events),
                               "predictions": dict(Counter(event["label"] for event in events))})
    events = [event for row in rows + bursts for event in row["audit"] if event["kind"] == "tool_result"]
    roles = {}
    for role in sorted({row["role"] for row in attempts}):
        selected = [row for row in attempts if row["role"] == role]
        roles[role] = {"attempts": len(selected), "estimated_usd": sum(row["estimated_usd"] for row in selected),
                       "latency": distribution([row["elapsed_seconds"] * 1000 for row in selected])}
    return {"functional": json.loads((agent / "summary.json").read_text()),
            "execution": json.loads((agent / "execution_stats.json").read_text()),
            "components": json.loads((base / "components/summary.json").read_text()),
            "review_kind": review["review_kind"], "review_counts": review_counts(rows, review),
            "semantic_accuracy": None, "independent_human_review": False,
            "first_stage_classification": classification,
            "executed_tools": {"n": len(events), "statuses": dict(Counter(event["status"] for event in events)),
                               "by_tool": dict(Counter(event["tool"] for event in events))},
            "model_roles": roles, "attempts_linked_to_saved_task": len(linked_attempts),
            "attempts_total": len(attempts), "all_usage_known": all(row["usage_known"] for row in attempts),
            "sse": [{"id": row["id"], "first_delta_ms": row["first_delta_ms"], "done_ms": row["elapsed_ms"]}
                    for row in rows if row["first_delta_ms"] is not None],
            "frozen_code_files_changed": changed,
            "artifact_sha256": {str(path.relative_to(base)): hashlib.sha256(path.read_bytes()).hexdigest()
                                for path in (agent / "functional.jsonl", agent / "bursts.jsonl",
                                             agent / "model_attempts.jsonl", agent / "frozen_cases.jsonl",
                                             agent / "frozen_rubrics.json", agent / "manifest.json",
                                             base / "assistant_review.json")}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    base = args.input.resolve()
    if not base.is_relative_to(ROOT / "reports"):
        parser.error("Input must be below reports/")
    result = analyze(base)
    if result["frozen_code_files_changed"]:
        raise ValueError("Code changed after the paid run; inspect frozen_code_files_changed")
    (base / "analysis.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    rows = read_jsonl(base / "agent/functional.jsonl")
    review = json.loads((base / "assistant_review.json").read_text(encoding="utf-8"))
    indexed = {row["id"]: row for row in review["cases"]}
    labels = {"meets_rubric": "满足本轮判据", "partial": "部分完成", "not_completed": "未完成",
              "appropriate_clarification": "合理澄清，但整组未完成"}
    lines = ["# 63题逐项复核", "", "按冻结判据进行的助手复核，非独立人工金标；不作为答案准确率。", "",
             "| 编号 | 问题 | 判定 | 观察 |", "| --- | --- | --- | --- |"]
    for row in rows:
        item = indexed[row["id"]]
        cells = [row["id"], row["question"], labels[item["verdict"]], item["note"]]
        lines.append("| " + " | ".join(cell.replace("|", "\\|").replace("\n", " ") for cell in cells) + " |")
    (base / "assistant_review.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"review": result["review_counts"], "tools": result["executed_tools"],
                      "model_roles": result["model_roles"], "linked_attempts": result["attempts_linked_to_saved_task"],
                      "changed_frozen_files": result["frozen_code_files_changed"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
