"""3000虚拟用户的短时模拟依赖诊断，服务与Locust分进程、同一物理机。"""
import argparse
import json
import os
import subprocess
import time
from pathlib import Path

import httpx
import psutil

from app.core.runtime import ROOT
from tests.performance.run_offline import HOST, distribution, server


def summarize_rows(rows):
    success = [row for row in rows if row["business_success"]]
    return {"requests": len(rows), "business_success": len(success),
            "http_429": sum(row["status"] == 429 for row in rows),
            "http_5xx": sum(row["status"] >= 500 for row in rows),
            "transport_failures": sum(row["status"] == 0 for row in rows),
            "degraded": sum(row.get("degraded") is True for row in rows),
            "completed_latency": distribution([row["elapsed_ms"] for row in success]),
            "all_latency": distribution([row["elapsed_ms"] for row in rows]),
            "send_start_span_seconds": max(row["started_offset_seconds"] for row in rows)
                                       - min(row["started_offset_seconds"] for row in rows) if rows else None}


def run_stage(output, users, seconds, spike, backend, load_python, tokens):
    name = f"{'spike' if spike else 'users'}_{users}"
    raw = output / (name + ".jsonl")
    env = dict(os.environ, TAYUNE_LOAD_OUTPUT=str(raw), TAYUNE_SCALE_USERS=str(users),
               TAYUNE_SCALE_SPIKE="1" if spike else "0", TAYUNE_SCALE_TOKENS=str(tokens))
    args = [str(load_python), "-m", "locust", "-f", "tests/performance/scale_locustfile.py", "--headless",
            "--host", HOST, "--users", str(users), "--spawn-rate", str(users if spike else 100),
            "--run-time", f"{seconds}s", "--stop-timeout", "35", "--only-summary", "--csv", str(output / name)]
    samples, stop_reason = [], None
    started = time.monotonic()
    with (output / (name + ".log")).open("x") as log:
        process = subprocess.Popen(args, cwd=ROOT, env=env, stdout=log, stderr=log)
        try:
            back = psutil.Process(backend.pid)
            generator = psutil.Process(process.pid)
            while process.poll() is None:
                try:
                    samples.append({"backend_rss_mib": back.memory_info().rss / 2**20,
                                    "backend_cpu": back.cpu_percent(), "generator_cpu": generator.cpu_percent(),
                                    "generator_rss_mib": generator.memory_info().rss / 2**20,
                                    "available_gib": psutil.virtual_memory().available / 2**30})
                except psutil.NoSuchProcess:
                    break
                if samples[-1]["available_gib"] < 1:
                    stop_reason = "available_memory_below_1GiB"
                    break
                if time.monotonic() - started > seconds + 50:
                    stop_reason = "load_generator_deadline"
                    break
                time.sleep(.5)
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=40)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    rows = [json.loads(line) for line in raw.read_text().splitlines()] if raw.exists() else []
    meta_path = Path(str(raw) + ".meta.json")
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    stats = httpx.get(HOST + "/benchmark/stats", trust_env=False, timeout=15).json()
    return {"users": users, "spike": spike, "planned_seconds": seconds, "wall_seconds": time.monotonic() - started,
            "exit_code": process.returncode, "stop_reason": stop_reason, "generator": meta, **summarize_rows(rows),
            "resources": {key: max(row[key] for row in samples) for key in
                          ("backend_rss_mib", "generator_rss_mib", "backend_cpu", "generator_cpu")} if samples else {},
            "server": stats}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--load-python", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / "reports") or output.exists():
        parser.error("Output must be a new reports directory")
    output.mkdir(parents=True)
    stages = []
    manifest = {"commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                "dependencies": "all simulated", "paid_calls": 0, "capacity": 4,
                "session_cap": 100, "isolation": "8012, own SQLite; same physical machine, separate processes",
                "plan": "3000 registration attempts, then explicitly seed3000; stages10/50/100/300/1000/3000 plus3000 spike",
                "limits": "20000 chats/stage; 1GiB free memory stop; stage seconds30 or60; spike25",
                "not_a_claim": "not real model throughput, real answer quality or public website capacity"}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2))
    with server(output / "server", module="tests.performance.scale_server") as backend:
        with httpx.Client(trust_env=False, timeout=30) as client:
            # 单独验证正式会话业务上限；不把预置会话说成注册成功。
            statuses = [client.post(HOST + "/api/session", json={}).status_code for _ in range(3000)]
            registration = {"attempts": len(statuses), "created": statuses.count(200),
                            "rejected": statuses.count(429), "other_statuses": sorted(set(statuses) - {200, 429})}
            (output / "registration.json").write_text(json.dumps(registration, indent=2))
            print("REGISTRATION", json.dumps(registration), flush=True)
            seed = client.post(HOST + "/benchmark/seed").json()
            (output / "seed.json").write_text(json.dumps(seed, indent=2))
            tokens = output / "server/synthetic_tokens.json"
            for users in (10, 50, 100, 300, 1000, 3000):
                item = run_stage(output, users, 60 if users == 3000 else 30, False, backend,
                                 args.load_python.absolute(), tokens)
                stages.append(item)
                (output / "stages.json").write_text(json.dumps(stages, indent=2))
                print("STAGE", json.dumps(item), flush=True)
                if item["stop_reason"] or item["http_5xx"] or item["transport_failures"] or not item["generator"]:
                    break
                time.sleep(8.2)
            if stages[-1]["users"] == 3000 and not stages[-1]["stop_reason"]:
                item = run_stage(output, 3000, 25, True, backend, args.load_python.absolute(), tokens)
                stages.append(item)
                print("SPIKE", json.dumps(item), flush=True)
            time.sleep(1)
            final = client.get(HOST + "/benchmark/stats").json()
            (output / "summary.json").write_text(json.dumps({"registration": registration, "stages": stages,
                                                            "final": final}, indent=2))


if __name__ == "__main__":
    main()
