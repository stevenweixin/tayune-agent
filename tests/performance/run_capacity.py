"""有界容量实验：模拟依赖持续压测，与真实依赖短时突发分别记录。"""
import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
import psutil

from tests.performance.run_offline import ROOT, HOST, distribution, server

QUESTIONS = [("product", "Do you sell jasmine tea?"), ("policy", "What is your return policy?")]
PLAN = [("original", 1, [1, 2, 4, 8]), ("naive", 4, [4, 4]),
        ("queued", 2, [2, 2, 8]), ("queued", 4, [4, 4, 8]), ("queued", 8, [8, 8, 16])]


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def sample(process):
    return {"rss_mb": process.memory_info().rss / 1024 ** 2, "cpu_percent": process.cpu_percent()}


def summarize(rows):
    success = [row for row in rows if row["business_success"]]
    return {"requests": len(rows), "successful": len(success),
            "rejected_429": sum(row["status"] == 429 for row in rows),
            "degraded_200": sum(row["status"] == 200 and row.get("degraded") is True for row in rows),
            "server_5xx": sum(row["status"] >= 500 for row in rows),
            "success_latency": distribution([row["elapsed_ms"] for row in success]),
            "all_latency": distribution([row["elapsed_ms"] for row in rows])}


def simulated(output, locust):
    results = []
    for profile, capacity in [("original", 1), ("naive", 4), ("queued", 2), ("queued", 4), ("queued", 8)]:
        label = f"{profile}_{capacity}"
        raw = output / (label + "_requests.jsonl")
        extra = ["--capacity", str(capacity)]
        with server(output / label, profile, "tests.performance.capacity_server", extra) as backend:
            env = dict(os.environ, TAYUNE_LOAD_OUTPUT=str(raw))
            command = [str(locust), "-f", str(ROOT / "tests/performance/capacity_locustfile.py"),
                       "--headless", "--host", HOST, "--users", "20", "--spawn-rate", "20",
                       "--run-time", "30s", "--stop-timeout", "10", "--only-summary"]
            samples = []
            with (output / (label + "_locust.log")).open("x") as log:
                generator = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=log)
                process = psutil.Process(backend.pid)
                started = time.perf_counter()
                try:
                    while generator.poll() is None:
                        samples.append(sample(process))
                        if time.perf_counter() - started > 65:
                            raise TimeoutError("Load generator deadline exceeded")
                        time.sleep(.5)
                    generator.wait(timeout=5)
                finally:
                    if generator.poll() is None:
                        generator.terminate()
                        try:
                            generator.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            generator.kill()
                            generator.wait()
            stats = httpx.get(HOST + "/benchmark/stats", trust_env=False).json()
            rows = [json.loads(line) for line in raw.read_text().splitlines()]
            result = {"profile": profile, "capacity": capacity, "users": 20, "target_seconds": 30,
                      "wall_seconds": time.perf_counter() - started, **summarize(rows), "service": stats,
                      "peak_rss_mb": max(item["rss_mb"] for item in samples),
                      "peak_cpu_percent": max(item["cpu_percent"] for item in samples)}
            result["successful_rps_nominal"] = result["successful"] / 30
            results.append(result)
            save(output / "summary.json", results)
            print("SIMULATED", label, result["successful"], result["degraded_200"], flush=True)
    return results


def proxy_check(kind, body):
    answer = body.get("answer", "").lower()
    sources = body.get("sources", [])
    safe = all(urlparse(item.get("url", "")).scheme == "https"
               and urlparse(item.get("url", "")).netloc == "tayunetea.com" for item in sources)
    if kind == "product":
        matched = any("jasmine" in (item.get("url", "") + item.get("title", "")).lower() for item in sources)
        matched = matched and ("usd" in answer or "$" in answer)
    else:
        matched = "opened tea" in answer and ("cannot" in answer or "can't" in answer)
    return (body.get("degraded") is False and bool(sources) and safe and matched
            and body.get("model_calls", 0) <= 4 and body.get("tool_count", 0) <= 4)


async def wave(users, index, backend_pid):
    rows, samples, checks = [], [], []
    process = psutil.Process(backend_pid)
    done = asyncio.Event()

    async def monitor():
        while not done.is_set():
            samples.append(sample(process))
            await asyncio.sleep(.25)

    async with httpx.AsyncClient(trust_env=False, timeout=100) as client:
        tokens = []
        for _ in range(users):
            response = await client.post(HOST + "/api/session", json={})
            response.raise_for_status()
            tokens.append(response.json()["session_token"])
        ready = asyncio.Event()

        async def ask(position):
            kind, question = QUESTIONS[(position + index) % 2]
            key = uuid.uuid4().hex
            await ready.wait()
            started = time.perf_counter()
            response = await client.post(HOST + "/api/chat", headers={"Authorization": "Bearer " + tokens[position]},
                                         json={"message": question, "request_id": key})
            elapsed = (time.perf_counter() - started) * 1000
            body = response.json()
            return {"position": position, "kind": kind, "question": question, "request_key": key,
                    "status": response.status_code, "elapsed_ms": elapsed, "result": body,
                    "degraded": body.get("degraded"),
                    "business_success": response.status_code == 200 and proxy_check(kind, body)}

        tasks = [asyncio.create_task(ask(position)) for position in range(users)]
        monitor_task = asyncio.create_task(monitor())
        started = time.perf_counter()
        ready.set()
        try:
            rows = await asyncio.gather(*tasks)
        finally:
            # 取消剩余客户端等待不等于供应商没有计费；调用方异常后不能自动续跑。
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            done.set()
            await monitor_task
        elapsed = time.perf_counter() - started
        task_ids = [row["result"].get("task_id") for row in rows if row["status"] == 200]
        checks.append({"case": "unique_task_ids", "passed": len(task_ids) == len(set(task_ids))})
        for row in rows:
            if row["status"] != 200:
                continue
            position = row["position"]
            headers = {"Authorization": "Bearer " + tokens[position]}
            task_id = row["result"]["task_id"]
            own = await client.get(HOST + "/api/local/tasks/" + task_id, headers=headers)
            wrong = await client.get(HOST + "/api/local/tasks/" + task_id,
                                     headers={"Authorization": "Bearer " + tokens[(position + 1) % users]}) if users > 1 else None
            checks.append({"case": "task_owner", "passed": own.status_code == 200
                           and (wrong is None or wrong.status_code == 404)})
            if not row["degraded"]:
                memory = await client.get(HOST + "/api/local/memory", headers=headers)
                history = memory.json().get("history", [])
                checks.append({"case": "session_history", "passed": len(history) == 2
                               and history[0]["text"] == row["question"]})
        stats = (await client.get(HOST + "/benchmark/stats")).json()
        checks.append({"case": "atomic_budget_and_tasks", "passed": stats["budget_count"] == stats["task_rows"]
                       and stats["active"] == 0})
    result = {"users": users, "wave": index, "burst_seconds": elapsed, **summarize(rows),
              "short_burst_completed_rps": sum(row["business_success"] for row in rows) / elapsed,
              "peak_rss_mb": max(item["rss_mb"] for item in samples),
              "peak_cpu_percent": max(item["cpu_percent"] for item in samples), "checks": checks, "service": stats}
    return rows, result


def real(output):
    results = []
    attempts, cost = 0, 0.0
    stable = True
    for profile, capacity, widths in PLAN:
        if attempts >= 120 or cost > .5:
            break
        if profile == "queued" and capacity == 8 and not stable:
            results.append({"profile": profile, "capacity": capacity, "skipped": "prior_within_capacity_unstable"})
            break
        label = f"{profile}_{capacity}"
        extra = ["--capacity", str(capacity), "--dependencies", "real", "--paid",
                 "--attempts", str(120 - attempts), "--usd", str(1 - cost)]
        with server(output / label, profile, "tests.performance.capacity_server", extra) as backend:
            for index, users in enumerate(widths):
                rows, result = asyncio.run(wave(users, index, backend.pid))
                result.update(profile=profile, capacity=capacity)
                with (output / (label + "_requests.jsonl")).open("a") as file:
                    for row in rows:
                        file.write(json.dumps(row, ensure_ascii=False) + "\n")
                results.append(result)
                save(output / "summary.json", results)
                print("REAL", label, users, "success", result["successful"], "degraded", result["degraded_200"],
                      "P95", result["success_latency"]["p95_ms"], flush=True)
                stats = result["service"]
                if stats["budget_stop"]:
                    break
                if profile == "queued" and users <= capacity:
                    p95 = result["success_latency"]["p95_ms"]
                    stable = stable and result["successful"] / users >= .95 and p95 is not None and p95 <= 15000
            stats = httpx.get(HOST + "/benchmark/stats", trust_env=False).json()
            attempts += stats["model_attempts"]
            cost += stats["estimated_usd"]
            save(output / "budget.json", {"model_attempts": attempts, "estimated_usd": cost,
                                          "stop": stats["budget_stop"]})
            if stats["budget_stop"]:
                break
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--locust", type=Path, required=True)
    parser.add_argument("--paid", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / "reports"):
        parser.error("Output must be below reports/")
    output.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for path in (ROOT / "tests/performance").glob("*.py"):
        hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    save(output / "manifest.json", {"plan": PLAN, "questions": QUESTIONS, "code_sha256": hashes,
         "paid": args.paid, "limits": {"physical_model_attempts": 120, "estimated_usd": 1},
         "hardware_ram_gb": psutil.virtual_memory().total / 1024 ** 3, "logical_cpus": psutil.cpu_count(),
         "python": sys.version, "reference_threshold": "within-capacity proxy>=95%, success P95<=15s; not a production SLA",
         "isolation": "8012, own SQLite, real Milvus/public catalog reads only; no production changes",
         "scope": "two simple English questions, real short bursts vs simulated 20-user 30s load"})
    simulated_path = output / "simulated"
    simulated_path.mkdir()
    simulated(simulated_path, args.locust.resolve())
    if args.paid:
        real_path = output / "real"
        real_path.mkdir()
        real(real_path)


if __name__ == "__main__":
    main()
