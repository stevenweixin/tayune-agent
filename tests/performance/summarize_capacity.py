"""从容量实验原始账本汇总，不将业务代理检查称为答案准确率。"""
import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from tests.performance.run_offline import ROOT, distribution


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    root = args.input.resolve()
    if not root.is_relative_to(ROOT / "reports"):
        parser.error("Input must be below reports/")
    simulated = json.loads((root / "simulated/summary.json").read_text())
    waves = json.loads((root / "real/summary.json").read_text())
    profiles = []
    for path in sorted((root / "real").glob("*_requests.jsonl")):
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        label = path.stem.removesuffix("_requests")
        profile, capacity = label.rsplit("_", 1)
        related = [item for item in waves if item["profile"] == profile and item["capacity"] == int(capacity)
                   and "service" in item]
        admitted = [row for row in rows if row["status"] == 200]
        passed = [row for row in rows if row["business_success"]]
        usage_path = root / "real" / label / "model_attempts.jsonl"
        usage = [json.loads(line) for line in usage_path.read_text().splitlines()] if usage_path.exists() else []
        checks = [check for item in related for check in item["checks"]]
        within = []
        offset = 0
        for item in related:
            # 原始账本按wave、position顺序写入；不重写原始记录来补造测量字段。
            batch = rows[offset:offset + item["users"]]
            offset += item["users"]
            if item["users"] <= int(capacity):
                within.extend(batch)
        profiles.append({"label": label, "submitted": len(rows), "http_200": len(admitted),
            "proxy_pass": len(passed), "degraded": sum(row.get("degraded") is True for row in admitted),
            "rejected_429": sum(row["status"] == 429 for row in rows),
            "http_5xx": sum(row["status"] >= 500 for row in rows),
            "success_latency": distribution([row["elapsed_ms"] for row in passed]),
            "model_latency": distribution([row["elapsed_seconds"] * 1000 for row in usage]),
            "model_attempts": len(usage), "actual_model_names": dict(Counter(row.get("actual_model") for row in usage)),
            "estimated_usd": sum(row.get("estimated_usd", 0) for row in usage),
            "unknown_usage": sum(not row.get("usage_known") for row in usage),
            "trace": dict(Counter(event for row in admitted for event in row["result"].get("trace", []))),
            "within_capacity": {"submitted": len(within), "proxy_pass": sum(row["business_success"] for row in within),
                                "success_latency": distribution([row["elapsed_ms"] for row in within if row["business_success"]])},
            "checks_passed": sum(check["passed"] for check in checks), "checks_total": len(checks),
            "last_service_stats": related[-1]["service"]})
    hashes = {}
    for path in sorted(root.rglob("*.jsonl")):
        hashes[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    result = {"simulated": simulated, "real_profiles": profiles, "real_waves": waves, "raw_sha256": hashes,
              "budget": json.loads((root / "real/budget.json").read_text()),
              "interpretation": "Operational proxy on two simple questions, not human answer accuracy or production SLA"}
    (root / "overview.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    for item in profiles:
        print(json.dumps({key: value for key, value in item.items() if key != "last_service_stats"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
