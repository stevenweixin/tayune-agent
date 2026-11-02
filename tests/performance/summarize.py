"""汇总已保存的原始证据；不发网络请求，不重新评判或改写原测试结果。"""
import argparse
import json
from collections import Counter
from pathlib import Path

from tests.performance.run_offline import distribution


def read_rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def summarize(base):
    rows, attempts = [], []
    for name in ("real_v1", "real_v2"):
        rows.extend(read_rows(base / name / "results.jsonl"))
        attempts.extend(read_rows(base / name / "model_attempts.jsonl"))
    categories = {}
    for category in sorted({row["category"] for row in rows}):
        group = [row for row in rows if row["category"] == category]
        categories[category] = {"n": len(group), "strict_contract_passes": sum(row["contract_pass"] for row in group),
                                "degraded": sum(row["result"].get("degraded", False) for row in group),
                                "latency": distribution([row["elapsed_ms"] for row in group])}
    classifier = Counter()
    tool_results = Counter()
    for row in rows:
        for event in row["audit"]:
            if event["kind"] == "classification":
                classifier[row["category"] + ":" + event["label"]] += 1
            if event["kind"] == "tool_result":
                tool_results[event.get("error") or event.get("status")] += 1
    positive = [row for row in rows if row["category"] in {"smoke", "product", "knowledge", "multi_turn"}]
    successful = [row for row in positive if row["contract_pass"]]
    expected = {"product": "in_scope", "knowledge": "in_scope", "restricted": "restricted", "out_of_scope": "out_of_scope"}
    classified = []
    for row in rows:
        if row["category"] in expected:
            event = next(item for item in row["audit"] if item["kind"] == "classification")
            classified.append({"id": row["id"], "expected": expected[row["category"]], "actual": event["label"]})
    restricted = [row for row in rows if row["category"] == "restricted"]
    no_tool_executions = sum(not any(event["kind"] == "tool_execute" for event in row["audit"]) for row in restricted)
    audit_files = [base / name / "results.jsonl" for name in ("real_v1", "real_v2")]
    import hashlib
    result = {"real": {"requests": len(rows), "physical_model_attempts": len(attempts),
        "http_200": sum(row["http_status"] == 200 for row in rows),
        "strict_contract_passes": sum(row["contract_pass"] for row in rows),
        "degraded": sum(row["result"].get("degraded", False) for row in rows),
        "end_to_end_latency": distribution([row["elapsed_ms"] for row in rows]),
        "successful_positive_latency": distribution([row["elapsed_ms"] for row in successful]),
        "sse_first_delta": distribution([row["first_delta_ms"] for row in rows if row.get("first_delta_ms") is not None]),
        "tokens": {key: sum(row["usage"][key] for row in attempts) for key in ("input_tokens", "output_tokens")},
        "conservative_estimated_usd": sum(row["estimated_usd"] for row in attempts),
        "returned_model_aliases": dict(Counter(row["actual_model"] for row in attempts)),
        "categories": categories, "tool_results": dict(tool_results), "classifier_distribution": dict(classifier),
        "classifier_exact_matches": sum(row["expected"] == row["actual"] for row in classified),
        "classifier_labelled_count": len(classified),
        "restricted_without_tool_execution": no_tool_executions,
        "stop_reason": "120-attempt cap; last request interrupted by evaluation budget, three planned turns unsent",
        "strict_metric_limit": "nonempty, domain, budget, public sources/refusal/redirect; NOT semantic accuracy",
        "semantic_review": "independent human review pending",
        "raw_sha256": {str(path.relative_to(base)): hashlib.sha256(path.read_bytes()).hexdigest() for path in audit_files}},
        "components": json.loads((base / "components_v1/summary.json").read_text()),
        "offline_contracts": json.loads((base / "offline_v1/contracts.json").read_text()),
        "load": json.loads((base / "offline_v1/load_summary.json").read_text())}
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    args = parser.parse_args()
    result = summarize(args.base)
    (args.base / "aggregate.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result["real"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
