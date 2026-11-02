"""用上轮5条CPU竞争轨迹的原始工具参数复测；真实检索，不调用DeepSeek。"""
import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

from dotenv import load_dotenv

from app.agent.knowledge_tool import build_knowledge_tool
from app.core.logger import setup_logging
from app.core.runtime import ROOT, Runtime


def load_pairs(baseline):
    pairs = []
    for name in ("real_v1", "real_v2"):
        folder = baseline / name
        with sqlite3.connect(f"file:{folder / 'sessions.sqlite3'}?mode=ro", uri=True) as db:
            for line in (folder / "results.jsonl").read_text().splitlines():
                row = json.loads(line)
                failed = [event for event in row["audit"] if event["kind"] == "tool_result"
                          and event.get("error") == "knowledge_unavailable"]
                if not failed:
                    continue
                saved = db.execute("SELECT checkpoint FROM task_runs WHERE id=?", (row["result"]["task_id"],)).fetchone()
                state = json.loads(saved[0])
                for message in state["messages"]:
                    if message["type"] != "ai":
                        continue
                    calls = message["data"].get("tool_calls", [])
                    knowledge_calls = [call for call in calls if call["name"] == "search_knowledge"]
                    if len(knowledge_calls) == 2 and any(call["id"] == failed[0]["step_id"] for call in knowledge_calls):
                        pairs.append({"case_id": row["id"], "args": [call["args"] for call in knowledge_calls],
                                      "baseline": {"successful_tools": 1, "failed_tools": 1}})
                        break
    if len(pairs) != 5:
        raise ValueError("Expected exactly five recorded CPU contention pairs")
    return pairs


async def execute(args):
    output = args.output.resolve()
    baseline = args.baseline.resolve()
    if not output.is_relative_to(ROOT / "reports") or not baseline.is_relative_to(ROOT / "reports"):
        raise ValueError("Evidence paths must be below reports/")
    output.mkdir(parents=True, exist_ok=False)
    pairs = load_pairs(baseline)
    frozen = json.dumps(pairs, ensure_ascii=False, indent=2)
    (output / "frozen_pairs.json").write_text(frozen, encoding="utf-8")
    setup_logging(output / "backend.log")
    load_dotenv(ROOT / ".env", override=False)
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    runtime = Runtime(session_path=output / "unused.sqlite3")
    rows = []
    try:
        _, retriever, reranker = await runtime.cpu.run(runtime.load_models)
        tool = build_knowledge_tool(retriever, reranker, runtime.cpu)
        for pair in pairs:
            started = time.perf_counter()
            results = await asyncio.gather(*(tool.ainvoke(params) for params in pair["args"]))
            row = {**pair, "elapsed_seconds": time.perf_counter() - started,
                   "results": [{"ok": item["ok"], "error": item.get("error"),
                                "passages": len(item.get("passages", []))} for item in results],
                   "queue_contract_pass": all(item["ok"] for item in results)}
            rows.append(row)
            with (output / "results.jsonl").open("a", encoding="utf-8") as file:
                file.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(json.dumps(row, ensure_ascii=False), flush=True)
        summary = {"pairs": len(rows), "tool_calls": len(rows) * 2,
                   "successful_tools": sum(item["ok"] for row in rows for item in row["results"]),
                   "nonempty_tools": sum(item["passages"] > 0 for row in rows for item in row["results"]),
                   "queue_contract_passes": sum(row["queue_contract_pass"] for row in rows),
                   "paid_model_calls": 0, "input_sha256": hashlib.sha256(frozen.encode()).hexdigest(),
                   "scope": "same recorded tool parameters; English filter also active; not a full answer accuracy test"}
        (output / "summary.json").write_text(json.dumps(summary, indent=2))
    finally:
        await runtime.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(execute(parser.parse_args()))


if __name__ == "__main__":
    main()
