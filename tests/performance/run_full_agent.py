"""完整固定Agent用例+正式4路准入突发；隔离SQLite，预算覆盖全部真实模型角色。"""
import argparse
import asyncio
import hashlib
import json
import os
import socket
import subprocess
import time
import uuid
from collections import Counter
from pathlib import Path

import httpx
import psutil
import uvicorn

from app.core.logger import setup_logging
from app.core.runtime import ROOT
from app.main import create_app
from tests.performance.capacity_server import SharedBudget
from tests.performance.full_agent_cases import conversation_evidence, review_rubric, suite, evaluate_contract
from tests.performance.run_offline import HOST, distribution
from tests.performance.run_real import EvaluationRuntime


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def summarize(rows):
    positive = [row for row in rows if row["category"] not in ("restricted", "out_of_scope")]
    completed = [row for row in positive if row["http_status"] == 200 and not row["result"].get("degraded", True)]
    return {"submitted": len(rows), "http_200": sum(row["http_status"] == 200 for row in rows),
            "rejected_429": sum(row["http_status"] == 429 for row in rows),
            "server_5xx": sum(row["http_status"] >= 500 for row in rows),
            "degraded": sum(row["result"].get("degraded") is True for row in rows),
            "proxy_passes": sum(row["contract_pass"] for row in rows),
            "all_latency": distribution([row["elapsed_ms"] for row in rows]),
            "positive_completed_latency": distribution([row["elapsed_ms"] for row in completed]),
            "categories": {category: {"n": sum(row["category"] == category for row in rows),
                                      "proxy_passes": sum(row["category"] == category and row["contract_pass"] for row in rows)}
                           for category in sorted({row["category"] for row in rows})},
            "tool_errors": dict(Counter(event.get("error") for row in rows for event in row["audit"]
                                        if event["kind"] == "tool_result" and event.get("error")))}


async def execute(args):
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / "reports") or output.exists():
        raise ValueError("Output must be a new directory below reports/")
    # 先绑定自己的端口；已占用则退出，不接管任何现有服务。
    sock = socket.socket()
    sock.bind(("127.0.0.1", 8012))
    output.mkdir(parents=True)
    setup_logging(output / "backend.log")
    for key in ("DEEPSEEK_MODEL", "TAYUNE_MAIN_MODEL", "TAYUNE_REVIEW_MODEL", "TAYUNE_SUMMARY_MODEL"):
        os.environ[key] = "deepseek-flash"
    os.environ.update(TAYUNE_FALLBACK_MODEL="", TAYUNE_MAX_CONCURRENT_CHATS="4",
                      TAYUNE_DAILY_REQUEST_LIMIT="100", TOKENIZERS_PARALLELISM="false")
    cases = suite()
    frozen = "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases)
    (output / "frozen_cases.jsonl").write_text(frozen, encoding="utf-8")
    save(output / "frozen_rubrics.json", {case["id"]: review_rubric(case) for case in cases})
    manifest = {"commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "dataset_sha256": hashlib.sha256(frozen.encode()).hexdigest(),
                "limits": {"submitted_chats": 75, "physical_model_attempts": 300, "estimated_usd": 1},
                "plan": "3 SSE smoke + frozen 60 cases + simultaneous width4 and width8 (formal capacity4)",
                "configured_model": "deepseek-flash", "production_model_unchanged": True,
                "price_basis": "Peak cache-miss input $0.30/M, output $1.20/M; conservative estimate, not bill",
                "price_source": "https://api-docs.deepseek.com/quick_start/pricing/",
                "review_kind": "assistant review against frozen rubrics, not independent human gold accuracy",
                "hardware": {"ram_gb": psutil.virtual_memory().total / 1024 ** 3, "logical_cpus": psutil.cpu_count()},
                "isolation": "8012 and own SQLite; production/environment/Tunnel/Milvus records unchanged",
                "code_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                                for base in (ROOT / "app", ROOT / "tests/performance") for path in base.rglob("*.py")
                                if not path.stem.endswith("_1")}}
    save(output / "manifest.json", manifest)
    budget = SharedBudget(output / "model_attempts.jsonl", max_attempts=300, max_usd=1)
    runtime = EvaluationRuntime(output, budget)
    app = create_app(runtime_factory=lambda: runtime)
    app.state.request_limiter.limits = {"/api/session": 10000, "/api/chat": 10000}
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False, proxy_headers=False))
    server_task = asyncio.create_task(server.serve(sockets=[sock]))
    functional, bursts = [], []
    children = []
    started = time.perf_counter()

    def record(case, status, body, bearer, elapsed, first_delta=None):
        audit, checkpoint = [], {}
        if body.get("task_id"):
            saved = runtime.runs.get(bearer, body["task_id"])
            checkpoint = saved.get("checkpoint") or {}
            audit = runtime.runs.audit(body["task_id"])
            body = {**body, "trace": (saved.get("result") or {}).get("trace", [])}
        row = {**case, "http_status": status, "elapsed_ms": elapsed, "first_delta_ms": first_delta,
               "result": body, "audit": audit, "physical_model_attempts": body.get("model_calls", 0),
               "review_focus": review_rubric(case), **conversation_evidence(checkpoint)}
        row.update(evaluate_contract(case, body, audit, checkpoint, status))
        return row

    async def ask(client, case, bearer, streaming=False):
        headers = {"Authorization": "Bearer " + bearer}
        body = {"message": case["question"], "request_id": uuid.uuid4().hex}
        started_request = time.perf_counter()
        first_delta, payload = None, {}
        if streaming:
            headers["Accept"] = "text/event-stream"
            async with client.stream("POST", HOST + "/api/chat", headers=headers, json=body) as response:
                status = response.status_code
                if status != 200:
                    payload = json.loads(await response.aread())
                else:
                    event = None
                    async for line in response.aiter_lines():
                        if line.startswith("event:"):
                            event = line[6:].strip()
                        elif line.startswith("data:"):
                            data = json.loads(line[5:].strip())
                            if event == "delta" and first_delta is None:
                                first_delta = (time.perf_counter() - started_request) * 1000
                            if event == "done":
                                payload = dict(runtime.runs.get(bearer, data["request_id"])["result"])
        else:
            response = await client.post(HOST + "/api/chat", headers=headers, json=body)
            status, payload = response.status_code, response.json()
        return record(case, status, payload, bearer, (time.perf_counter() - started_request) * 1000, first_delta)

    async def token(client):
        response = await client.post(HOST + "/api/session", json={})
        response.raise_for_status()
        return response.json()["session_token"]

    try:
        while not server.started:
            if server_task.done():
                await server_task
                raise RuntimeError("Isolated server failed to start")
            if time.perf_counter() - started > 180:
                raise TimeoutError("Startup deadline exceeded")
            await asyncio.sleep(.2)
        manifest.update(startup_seconds=time.perf_counter() - started, versions=runtime.versions,
                        actual_admission_capacity=app.state.service.requests.capacity)
        save(output / "manifest.json", manifest)
        async with httpx.AsyncClient(trust_env=False, timeout=105) as client:
            sessions, last = {}, {}
            smoke_failed = 0
            for index, case in enumerate(cases):
                if budget.stopped:
                    break
                group = case["session_group"] or case["id"]
                if group not in sessions:
                    sessions[group] = await token(client)
                if group in last:
                    await asyncio.sleep(max(0, 8.2 - (time.monotonic() - last[group])))
                last[group] = time.monotonic()
                row = await ask(client, case, sessions[group], streaming=index < 3)
                functional.append(row)
                with (output / "functional.jsonl").open("a", encoding="utf-8") as file:
                    file.write(json.dumps(row, ensure_ascii=False) + "\n")
                print(json.dumps({"case": case["id"], "proxy": row["contract_pass"],
                                  "degraded": row["result"].get("degraded"), "seconds": round(row["elapsed_ms"] / 1000, 2),
                                  "attempts": budget.attempts, "usd": budget.estimated_usd}), flush=True)
                if index < 3 and (row["http_status"] != 200 or row["result"].get("degraded")):
                    smoke_failed += 1
                if smoke_failed >= 2:
                    budget.stopped = budget.stopped or "smoke_infrastructure_failed"
                    break
            # 同一正式Service继续突发测试，不注入实验准入器或CPU适配层。
            if len(functional) == 63 and not budget.stopped:
                for width in (4, 8):
                    if budget.stopped:
                        break
                    bearers = [await token(client) for _ in range(width)]
                    burst_cases = [{"id": f"burst-{width}-{index}", "question": "Do you sell jasmine tea?" if index % 2 == 0
                                    else "What is your return policy?", "category": "burst", "session_group": None}
                                   for index in range(width)]
                    release = asyncio.Event()

                    async def simultaneous(case, bearer):
                        await release.wait()
                        return await ask(client, case, bearer)

                    children = [asyncio.create_task(simultaneous(case, bearer)) for case, bearer in zip(burst_cases, bearers)]
                    burst_start = time.perf_counter()
                    release.set()
                    rows = await asyncio.gather(*children)
                    elapsed = time.perf_counter() - burst_start
                    for row in rows:
                        with (output / "bursts.jsonl").open("a", encoding="utf-8") as file:
                            file.write(json.dumps(row, ensure_ascii=False) + "\n")
                    item = {"width": width, "wall_seconds": elapsed, **summarize(rows)}
                    bursts.append(item)
                    print("BURST", json.dumps(item), flush=True)
            service = app.state.service
            save(output / "execution_stats.json", {"capacity": service.requests.capacity,
                 "peak_active": service.requests.peak, "remaining_active": service.requests.active,
                 "owners_remaining": len(service.requests.owners), "cpu_peak_pending": runtime.cpu.peak_pending,
                 "tasks": runtime.sessions.db.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0],
                 "daily_count": runtime.sessions.db.execute("SELECT SUM(count) FROM budget").fetchone()[0],
                 "task_statuses": dict(runtime.sessions.db.execute("SELECT status,COUNT(*) FROM task_runs GROUP BY status").fetchall())})
    finally:
        for child in children:
            if not child.done():
                child.cancel()
        await asyncio.gather(*children, return_exceptions=True)
        server.should_exit = True
        await server_task
        sock.close()
        summary = {"functional": summarize(functional), "bursts": bursts,
                   "functional_plan_completed": len(functional) == 63, "burst_plan_completed": len(bursts) == 2,
                   "model_attempts": budget.attempts, "estimated_usd": budget.estimated_usd, "stop_reason": budget.stopped,
                   "usage_known": all(row["usage_known"] for row in budget.records),
                   "tokens": {key: sum(row.get("usage", {}).get(key, 0) for row in budget.records)
                              for key in ("input_tokens", "output_tokens")},
                   "semantic_accuracy": None, "independent_human_review": "not performed"}
        save(output / "summary.json", summary)
        print("FINAL", json.dumps(summary), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--paid", action="store_true")
    args = parser.parse_args()
    if not args.paid:
        parser.error("Requires authorization for up to 75 submissions / 300 physical attempts / estimated US$1")
    asyncio.run(execute(args))


if __name__ == "__main__":
    main()
