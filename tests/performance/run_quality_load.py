"""相同24题在单路/4路运行，真实依赖；最多48问/192次物理尝试/估价USD1。"""
import argparse
import asyncio
import hashlib
import json
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import psutil
import uvicorn

from app.core.logger import setup_logging
from app.core.runtime import ROOT
from app.main import create_app
from tests.performance.capacity_server import SharedBudget
from tests.performance.full_agent_cases import conversation_evidence, evaluate_contract
from tests.performance.quality_cases import case_groups, quality_cases
from tests.performance.run_full_agent import summarize
from tests.performance.run_offline import HOST
from tests.performance.run_real import EvaluationRuntime


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


async def execute(output):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 8012))
    output.mkdir(parents=True)
    setup_logging(output / "backend.log")
    for role in ("DEEPSEEK_MODEL", "TAYUNE_MAIN_MODEL", "TAYUNE_REVIEW_MODEL", "TAYUNE_SUMMARY_MODEL"):
        os.environ[role] = "deepseek-flash"
    os.environ.update(TAYUNE_FALLBACK_MODEL="", TAYUNE_DAILY_REQUEST_LIMIT="100",
                      TAYUNE_MAX_CONCURRENT_CHATS="4", TOKENIZERS_PARALLELISM="false")
    cases = quality_cases()
    frozen = json.dumps(cases, ensure_ascii=False, indent=2)
    (output / "frozen_cases.json").write_text(frozen, encoding="utf-8")
    manifest = {"commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                "case_sha256": hashlib.sha256(frozen.encode()).hexdigest(),
                "cases_per_profile": 24, "physical_attempt_limit": 192, "estimated_usd_limit": 1,
                "profiles": ["sequential", "concurrent4"], "formal_admission_capacity": 4,
                "model": "deepseek-flash", "price_source": "https://api-docs.deepseek.com/quick_start/pricing/",
                "isolation": "8012/own SQLite; real local models/Milvus reads/public catalog/DeepSeek",
                "changes": "only isolated IP/daily limits; model roles overridden in process, production unchanged",
                "quality_kind": "known-case regression, frozen business criteria, independent human gold absent",
                "comparison_limits": "sequential first then concurrent; model nondeterminism/cache/time drift not controlled",
                "code_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                                for base in (ROOT / "app", ROOT / "tests/performance") for path in base.rglob("*.py")
                                if not path.stem.endswith("_1")}}
    save(output / "manifest.json", manifest)
    budget = SharedBudget(output / "model_attempts.jsonl", max_attempts=192, max_usd=1)
    runtime = EvaluationRuntime(output, budget)
    app = create_app(runtime_factory=lambda: runtime)
    app.state.request_limiter.limits = {"/api/session": 10000, "/api/chat": 10000}
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False, proxy_headers=False))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    rows, profiles, workers = [], [], []
    started = time.monotonic()
    try:
        while not server.started:
            if serving.done():
                await serving
                raise RuntimeError("Isolated startup failed")
            if time.monotonic() - started > 180:
                raise TimeoutError("Startup exceeded180s")
            await asyncio.sleep(.2)
        manifest.update(versions=runtime.versions, startup_seconds=time.monotonic() - started,
                        rss_mib=psutil.Process().memory_info().rss / 2**20)
        save(output / "manifest.json", manifest)
        async with httpx.AsyncClient(trust_env=False, timeout=105) as client:
            for profile, width in (("sequential", 1), ("concurrent4", 4)):
                if budget.stopped:
                    break
                profile_rows = []
                gate = asyncio.Semaphore(width)
                app.state.service.requests.peak = 0

                async def group_run(group):
                    if budget.stopped:
                        return
                    session = await client.post(HOST + "/api/session", json={})
                    session.raise_for_status()
                    bearer = session.json()["session_token"]
                    previous = None
                    for case in group:
                        if previous is not None:
                            await asyncio.sleep(max(0, 8.2 - (time.monotonic() - previous)))
                        async with gate:
                            if budget.stopped:
                                return
                            previous = time.monotonic()
                            started_request = time.perf_counter()
                            first_delta, payload, delta_text = None, {}, ""
                            headers = {"Authorization": "Bearer " + bearer}
                            body = {"message": case["question"], "request_id": uuid.uuid4().hex}
                            streaming = case["id"] in {"knowledge-13", "product-03"}
                            try:
                                if streaming:
                                    headers["Accept"] = "text/event-stream"
                                    async with client.stream("POST", HOST + "/api/chat", headers=headers, json=body) as response:
                                        status = response.status_code
                                        if status != 200:
                                            payload = json.loads(await response.aread())
                                        else:
                                            event = ""
                                            async for line in response.aiter_lines():
                                                if line.startswith("event:"):
                                                    event = line[6:].strip()
                                                elif line.startswith("data:"):
                                                    data = json.loads(line[5:])
                                                    if event == "delta":
                                                        if first_delta is None:
                                                            first_delta = (time.perf_counter() - started_request) * 1000
                                                        delta_text += data["text"]
                                                    if event == "done":
                                                        payload = dict(runtime.runs.get(bearer, data["request_id"])["result"])
                                else:
                                    response = await client.post(HOST + "/api/chat", headers=headers, json=body)
                                    status, payload = response.status_code, response.json()
                            except httpx.HTTPError as error:
                                status, payload = 0, {"error_type": type(error).__name__}
                                budget.stopped = budget.stopped or "client_transport_failure"
                            elapsed = (time.perf_counter() - started_request) * 1000
                            audit, checkpoint = [], {}
                            if payload.get("task_id"):
                                saved = runtime.runs.get(bearer, payload["task_id"])
                                audit = runtime.runs.audit(payload["task_id"])
                                checkpoint = saved.get("checkpoint") or {}
                            row = {**case, "profile": profile, "http_status": status, "result": payload,
                                   "elapsed_ms": elapsed, "first_delta_ms": first_delta,
                                   "within_deadline": elapsed <= case["deadline_seconds"] * 1000,
                                   "stream_text_matches_answer": delta_text == payload.get("answer") if streaming and status == 200 else None,
                                   "audit": audit, **conversation_evidence(checkpoint)}
                            row.update(evaluate_contract(case, payload, audit, checkpoint, status))
                            rows.append(row)
                            profile_rows.append(row)
                            with (output / "answers.jsonl").open("a", encoding="utf-8") as file:
                                file.write(json.dumps(row, ensure_ascii=False) + "\n")
                            print(json.dumps({"profile": profile, "case": case["id"], "http": status,
                                  "proxy": row["contract_pass"], "degraded": payload.get("degraded"),
                                  "seconds": round(elapsed / 1000, 2), "model_attempts": budget.attempts,
                                  "estimated_usd": budget.estimated_usd}), flush=True)

                groups = case_groups(cases)
                if width == 1:
                    for group in groups:
                        await group_run(group)
                else:
                    workers = [asyncio.create_task(group_run(group)) for group in groups]
                    await asyncio.gather(*workers)
                profiles.append({"profile": profile, **summarize(profile_rows),
                                 "peak_active": app.state.service.requests.peak,
                                 "within15s": sum(row["within_deadline"] for row in profile_rows)})
                save(output / "profiles.json", profiles)
                if len(profile_rows) != 24:
                    break
        save(output / "execution_stats.json", {"active": app.state.service.requests.active,
             "owners": len(app.state.service.requests.owners), "cpu_peak_pending": runtime.cpu.peak_pending,
             "task_statuses": dict(runtime.sessions.db.execute("SELECT status,COUNT(*) FROM task_runs GROUP BY status").fetchall()),
             "daily_count": runtime.sessions.db.execute("SELECT COALESCE(SUM(count),0) FROM budget").fetchone()[0]})
    finally:
        for task in workers:
            if not task.done():
                task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        server.should_exit = True
        await serving
        sock.close()
        save(output / "summary.json", {"profiles": profiles, "requests": len(rows), "model_attempts": budget.attempts,
             "estimated_usd": budget.estimated_usd, "usage_known": all(row["usage_known"] for row in budget.records),
             "stop_reason": budget.stopped, "semantic_accuracy": None, "review": "pending all-case rubric review"})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--paid", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    if not args.paid or not output.is_relative_to(ROOT / "reports") or output.exists():
        parser.error("Requires new reports directory and authorized --paid48/192/USD1")
    asyncio.run(execute(output))


if __name__ == "__main__":
    main()
