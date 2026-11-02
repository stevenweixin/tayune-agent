"""汇总修复前后同ID探针；原始结果不改写，预算截停案例单独剔除比较。"""
import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from app.core.runtime import ROOT
from tests.performance.run_offline import distribution


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def summarize(items):
    events = [event for row in items for event in row["audit"]]
    return {"requests": len(items), "passes": sum(row["contract_pass"] for row in items),
            "degraded": sum(row["result"].get("degraded", False) for row in items),
            "latency": distribution([row["elapsed_ms"] for row in items]),
            "positive_pass_latency": distribution([row["elapsed_ms"] for row in items
                if row["contract_pass"] and row["category"] in {"smoke", "product", "knowledge", "multi_turn"}]),
            "tool_errors": dict(Counter(event["error"] for event in events
                if event["kind"] == "tool_result" and event.get("error"))),
            "categories": {category: {"n": sum(row["category"] == category for row in items),
                "passes": sum(row["category"] == category and row["contract_pass"] for row in items)}
                for category in sorted({row["category"] for row in items})},
            "answers_with_cjk_characters": [row["id"] for row in items
                if any("\u4e00" <= char <= "\u9fff" for char in row["result"].get("answer", ""))]}


def compare(baseline, retest):
    before = rows(baseline / "real_v1/results.jsonl") + rows(baseline / "real_v2/results.jsonl")
    after = rows(retest / "real/results.jsonl")
    old_summary = json.loads((baseline / "real_v2/summary.json").read_text())
    new_summary = json.loads((retest / "real/summary.json").read_text())
    old_manifest = json.loads((baseline / "real_v2/manifest.json").read_text())
    new_manifest = json.loads((retest / "real/manifest.json").read_text())
    if old_manifest["dataset_sha256"] != new_manifest["dataset_sha256"]:
        raise ValueError("Frozen datasets differ; do not compare as matched cases")
    censored = set()
    if old_summary["stop_reason"] == "attempt_or_cost_limit":
        censored.add(before[-1]["id"])
    if new_summary["stop_reason"] == "attempt_or_cost_limit":
        censored.add(after[-1]["id"])
    old_map, new_map = {row["id"]: row for row in before}, {row["id"]: row for row in after}
    common = [row["id"] for row in before if row["id"] in new_map and row["id"] not in censored]
    matched = {"n": len(common), "before": summarize([old_map[key] for key in common]),
               "after": summarize([new_map[key] for key in common]), "censored_ids": sorted(censored),
               "improved_proxy_ids": [key for key in common if not old_map[key]["contract_pass"] and new_map[key]["contract_pass"]],
               "regressed_proxy_ids": [key for key in common if old_map[key]["contract_pass"] and not new_map[key]["contract_pass"]]}
    calls = rows(retest / "real/model_attempts.jsonl")
    files = [retest / "real/results.jsonl", retest / "knowledge_replay_v2/results.jsonl"]
    return {"before": summarize(before), "after": summarize(after), "matched": matched,
            "model_attempts": len(calls), "estimated_usd": sum(row["estimated_usd"] for row in calls),
            "tokens": {key: sum(row["usage"][key] for row in calls) for key in ("input_tokens", "output_tokens")},
            "usage_known": all(row["usage_known"] for row in calls),
            "model_aliases": sorted({row["actual_model"] for row in calls}),
            "code_fingerprint": new_manifest["versions"]["code"],
            "dataset_sha256": new_manifest["dataset_sha256"],
            "raw_sha256": {str(path.relative_to(retest)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files},
            "queue_replay": json.loads((retest / "knowledge_replay_v2/summary.json").read_text()),
            "semantic_accuracy": None, "independent_human_review": "pending",
            "comparison_limits": "one stochastic run per version, external catalogue not frozen; contract is not semantic accuracy"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--retest", type=Path, required=True)
    args = parser.parse_args()
    baseline, retest = args.baseline.resolve(), args.retest.resolve()
    if not baseline.is_relative_to(ROOT / "reports") or not retest.is_relative_to(ROOT / "reports"):
        parser.error("Evidence paths must be below reports/")
    result = compare(baseline, retest)
    (retest / "comparison.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
