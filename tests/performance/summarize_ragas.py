"""只读汇总已产生的RAGAS结果，生成私有逐题中文报告；不会发起模型调用。"""
import argparse
import json
from collections import Counter
from pathlib import Path

from tests.performance.run_ragas import METRICS

LABELS = {"faithfulness": "忠实度", "answer_relevancy": "回答相关性",
          "context_precision": "上下文平均精度", "context_recall": "参考声明覆盖率"}


def analyze(directory):
    manifest = json.loads((directory / "manifest.json").read_text())
    samples = json.loads((directory / "frozen_dataset.json").read_text())
    summary = json.loads((directory / "summary.json").read_text())
    results = [json.loads(line) for line in (directory / "scores.jsonl").read_text().splitlines()]
    raw = [json.loads(line) for line in (directory / "judge_responses.jsonl").read_text().splitlines()]
    lookup = {(r["sample_id"], r["metric"]): r for r in results}
    statuses = dict(Counter(row["status"] for row in results))
    usage = {key: sum(row["response"].get("usage", {}).get(key, 0) for row in raw)
             for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
    metadata = {"samples": len(samples), "case_ids": len({s["case_id"] for s in samples}),
                "distinct_original_questions": len({s["original_question"] for s in samples}),
                "status_counts": statuses, "scores": len(results), "expected_scores": len(samples) * len(METRICS),
                "token_usage": usage, "judge_response_models": dict(Counter(row["response"].get("model") for row in raw)),
                "observed_finish_reasons": dict(Counter(choice.get("finish_reason") for row in raw
                                                        for choice in row["response"].get("choices", []))),
                "budget": summary["budget"], "archive_commit": manifest["archive_commit"]}
    (directory / "analysis.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
    lines = ["# RAGAS逐题结果", "", "历史归档回答，不是本次重新调用Agent。分数不是答案准确率。",
             "参考答案为回溯整理稿，尚无独立人工金标审核。多轮项是单轮文本投影，不是原生多轮Agent评测。", ""]
    for sample in samples:
        lines += ["## " + sample["sample_id"], "", "问题：" + sample["original_question"],
                  "", "工具证据组数：" + str(len(sample["retrieved_contexts"])), "",
                  "| 指标 | 分数 | 状态 |", "| --- | ---: | --- |"]
        for metric in METRICS:
            row = lookup.get((sample["sample_id"], metric), {"score": None, "status": "not_run"})
            score = f"{row['score']:.4f}" if row["score"] is not None else "N/A"
            lines.append(f"| {LABELS[metric]} | {score} | {row['status']} |")
        lines += ["", "### 原始回答", "", sample["response"], "", "### 参考与限制", "",
                  sample["reference"] or "没有正向事实参考：本题按证据不足或指代未建立单独诊断。",
                  "", sample["reference_basis"], ""]
    (directory / "CASE_RESULTS_ZH.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    analyze(parser.parse_args().input.resolve())
