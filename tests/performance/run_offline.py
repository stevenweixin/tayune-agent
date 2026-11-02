"""运行隔离HTTP压测与故障注入；不会请求真实模型或修改生产配置。"""
import argparse
import asyncio
import json
import math
import os
import socket
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import httpx
import psutil

ROOT = Path(__file__).resolve().parents[2]
HOST = "http://127.0.0.1:8012"


def distribution(values):
    """最近秩分位数；空样本返回None，不把拒绝的耗时混进成功耗时。"""
    ordered = sorted(values)
    if not ordered:
        return {"n": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}
    return {"n": len(ordered), "p50_ms": ordered[math.ceil(len(ordered) * .50) - 1],
            "p95_ms": ordered[math.ceil(len(ordered) * .95) - 1], "max_ms": ordered[-1]}


@contextmanager
def server(output, profile="diagnostic"):
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 8012)) == 0:
            raise RuntimeError("8012 is occupied; refusing to stop an existing service")
    log = (output.parent / (output.name + "-console.log")).open("x", encoding="utf-8")
    process = subprocess.Popen([sys.executable, "-m", "tests.performance.server", "--output", str(output),
                                "--profile", profile], cwd=ROOT, stdout=log, stderr=log)
    try:
        with httpx.Client(trust_env=False, timeout=2) as client:
            for _ in range(120):
                if process.poll() is not None:
                    raise RuntimeError("Isolated server exited; inspect its console log")
                try:
                    if client.get(HOST + "/health/live").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.25)
            else:
                raise TimeoutError("Isolated startup exceeded 30 seconds")
        yield process
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        log.close()


def token(client):
    response = client.post(HOST + "/api/session", json={})
    response.raise_for_status()
    return response.json()["session_token"]


def post_chat(client, bearer, request_id=None, message="Show an oolong under 30 USD.", **kwargs):
    return client.post(HOST + "/api/chat", headers={"Authorization": "Bearer " + bearer},
                       json={"message": message, "request_id": request_id or uuid.uuid4().hex}, **kwargs)


async def race(bearer):
    async with httpx.AsyncClient(trust_env=False, timeout=20) as client:
        tasks = [client.post(HOST + "/api/chat", headers={"Authorization": "Bearer " + bearer},
                             json={"message": "Show one oolong.", "request_id": uuid.uuid4().hex}) for _ in range(2)]
        return await asyncio.gather(*tasks)


def contracts(output):
    results = []

    def record(name, passed, observed):
        results.append({"case": name, "passed": bool(passed), "observed": observed})

    with server(output / "guards", "production"), httpx.Client(trust_env=False, timeout=20) as client:
        response = client.post(HOST + "/api/session", headers={"Origin": "https://untrusted.example"})
        record("origin_rejected", response.status_code == 403, response.status_code)
        sessions = [client.post(HOST + "/api/session").status_code for _ in range(6)]
        record("session_ip_limit", sessions == [200] * 5 + [429], sessions)
        response = client.post(HOST + "/api/chat", json={"message": "hello"})
        record("missing_bearer", response.status_code == 401, response.status_code)
        response = post_chat(client, "unknown")
        record("unknown_session", response.status_code == 401, response.status_code)
        response = client.post(HOST + "/api/chat", content=b"x" * 8193)
        record("oversized_body", response.status_code == 413, response.status_code)
        response = post_chat(client, "unknown", message="x" * 501)
        record("oversized_question", response.status_code == 422, response.status_code)

    with server(output / "faults"), httpx.Client(trust_env=False, timeout=20) as client:
        bearer = token(client)
        key = uuid.uuid4().hex
        first = post_chat(client, bearer, key)
        first_body = first.json()
        record("successful_grounded_answer", first.status_code == 200 and not first_body.get("degraded")
               and "USD 20.00" in first_body.get("answer", ""), first_body.get("task_status"))
        before = client.get(HOST + "/benchmark/stats").json()["provider_attempts"]
        replay = post_chat(client, bearer, key)
        after = client.get(HOST + "/benchmark/stats").json()["provider_attempts"]
        record("idempotency_no_extra_model", replay.status_code == 200 and before == after
               and replay.json().get("task_id") == first_body.get("task_id"), {"extra_model_attempts": after - before})
        conflict = post_chat(client, bearer, key, message="Different question")
        record("idempotency_conflict", conflict.status_code == 409, conflict.status_code)
        cooldown = post_chat(client, bearer)
        record("session_cooldown", cooldown.status_code == 429, cooldown.status_code)
        responses = asyncio.run(race(token(client)))
        statuses = sorted(response.status_code for response in responses)
        record("concurrent_busy_rejection", statuses == [200, 429], statuses)
        for fault, expected_attempts in [("model_auth", 1), ("model_transport", 2), ("invalid_json", 2),
                                         ("tool_down", 2), ("unknown_tool", 2), ("guessed_id", 2)]:
            client.post(HOST + "/benchmark/fault/" + fault).raise_for_status()
            before = client.get(HOST + "/benchmark/stats").json()["provider_attempts"]
            response = post_chat(client, token(client))
            body = response.json()
            attempts = client.get(HOST + "/benchmark/stats").json()["provider_attempts"] - before
            record(fault, response.status_code == 200 and body.get("degraded") is True
                   and attempts == expected_attempts, {"http": response.status_code,
                                                      "degraded": body.get("degraded"), "attempts": attempts})
        client.post(HOST + "/benchmark/fault/none").raise_for_status()
        bearer = token(client)
        key = uuid.uuid4().hex
        started = time.perf_counter()
        first_delta = None
        done = None
        with client.stream("POST", HOST + "/api/chat", headers={"Authorization": "Bearer " + bearer,
                           "Accept": "text/event-stream"}, json={"message": "Show one oolong.", "request_id": key}) as response:
            for line in response.iter_lines():
                if line.startswith("event: delta") and first_delta is None:
                    first_delta = (time.perf_counter() - started) * 1000
                if line.startswith("event: done"):
                    done = (time.perf_counter() - started) * 1000
        record("sse_verified_delivery", first_delta is not None and done is not None and done > first_delta,
               {"first_delta_ms": first_delta, "done_ms": done, "native_model_stream": False})
        recovered = post_chat(client, token(client)).json()
        record("fault_recovery", recovered.get("degraded") is False, recovered.get("task_status"))
        (output / "faults_stats.json").write_text(json.dumps(client.get(HOST + "/benchmark/stats").json(), indent=2))
    return results


def load_stages(output, locust, seconds):
    stages = []
    for users in (1, 2, 5, 10, 20):
        stage = output / f"users_{users}"
        raw = output / f"users_{users}_requests.jsonl"
        with server(stage) as backend:
            env = dict(os.environ, TAYUNE_LOAD_OUTPUT=str(raw))
            args = [str(locust), "-f", str(ROOT / "tests/performance/locustfile.py"), "--headless", "--host", HOST,
                    "--users", str(users), "--spawn-rate", str(users), "--run-time", f"{seconds}s",
                    "--stop-timeout", "10", "--csv", str(output / f"users_{users}"), "--only-summary"]
            with (output / f"users_{users}_locust.log").open("x") as log:
                started = time.perf_counter()
                generator = subprocess.Popen(args, cwd=ROOT, env=env, stdout=log, stderr=log)
                samples = []
                try:
                    backend_process = psutil.Process(backend.pid)
                    generator_process = psutil.Process(generator.pid)
                    while generator.poll() is None:
                        try:
                            samples.append({"backend_rss_mb": backend_process.memory_info().rss / 1024 ** 2,
                                            "backend_cpu_percent": backend_process.cpu_percent(),
                                            "generator_cpu_percent": generator_process.cpu_percent()})
                        except psutil.NoSuchProcess:
                            # 正常退出与采样可能恰好交错，保留已有样本并让wait取得退出码。
                            break
                        time.sleep(.5)
                        if time.perf_counter() - started > seconds + 35:
                            raise TimeoutError("Load generator exceeded bounded run time")
                    generator.wait(timeout=10)
                finally:
                    # 中断或采样异常也回收自己的压测进程，不遗留后台请求。
                    if generator.poll() is None:
                        generator.terminate()
                        try:
                            generator.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            generator.kill()
                            generator.wait()
                wall = time.perf_counter() - started
            stats = httpx.get(HOST + "/benchmark/stats", trust_env=False).json()
            rows = [json.loads(line) for line in raw.read_text().splitlines()]
            success = [row for row in rows if row["business_success"]]
            summary = {"users": users, "target_seconds": seconds, "wall_seconds": wall,
                       "requests": len(rows), "successful": len(success),
                       "rejected_429": sum(row["status"] == 429 for row in rows),
                       "server_5xx": sum(row["status"] >= 500 for row in rows),
                       "business_success_rate": len(success) / len(rows) if rows else None,
                       "offered_rps": len(rows) / seconds, "successful_rps": len(success) / seconds,
                       "all_latency": distribution([row["elapsed_ms"] for row in rows]),
                       "success_latency": distribution([row["elapsed_ms"] for row in success]),
                       "backend_peak_rss_mb": max(row["backend_rss_mb"] for row in samples),
                       "backend_peak_cpu_percent": max(row["backend_cpu_percent"] for row in samples),
                       "generator_peak_cpu_percent": max(row["generator_cpu_percent"] for row in samples),
                       "service": stats, "generator_exit": generator.returncode}
            stages.append(summary)
            print(json.dumps(summary), flush=True)
            (output / "load_summary.json").write_text(json.dumps(stages, indent=2))
    return stages


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--locust", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=30, choices=range(10, 61))
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / "reports"):
        parser.error("Output must be below reports/")
    output.mkdir(parents=True, exist_ok=False)
    manifest = {"commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "dependencies": "simulated", "isolation": "loopback8012, separate SQLite, no production writes",
                "python": sys.version, "physical_memory_gb": psutil.virtual_memory().total / 1024 ** 3,
                "model_delay_seconds": .25, "tool_delay_seconds": .05,
                "profile": "diagnostic: IP/daily limits raised only in isolated app; global lock and cooldown unchanged"}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2))
    results = contracts(output)
    (output / "contracts.json").write_text(json.dumps(results, indent=2))
    print("Contracts:", sum(row["passed"] for row in results), "/", len(results), flush=True)
    load_stages(output, args.locust.resolve(), args.seconds)


if __name__ == "__main__":
    main()
