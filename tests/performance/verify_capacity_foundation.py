"""第一阶段容量TCP验收：正式注册/排队/分道，依赖全模拟，无付费调用。"""
import argparse
import asyncio
import hashlib
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import httpx

from app.core.concurrency import read_capacity_settings
from app.core.cpu_worker import CPUQueue
from app.core.runtime import ROOT
from app.main import create_app
from tests.performance.run_offline import HOST, server
from tests.performance.run_scale import run_stage
from tests.performance.scale_server import ScaleRuntime


class FoundationRuntime(ScaleRuntime):
    def __init__(self, output):
        super().__init__(output)
        settings = read_capacity_settings()
        self.max_sessions = settings["max_sessions"]
        self.max_concurrent_chats = settings["max_concurrent_chats"]
        self.max_waiting_chats = settings["max_waiting_chats"]
        self.admission_wait_seconds = settings["admission_wait_seconds"]
        self.classifier_cpu = CPUQueue(max_pending=32, timeout=5, name="classifier")

    async def close(self):
        await self.classifier_cpu.close()
        await super().close()


def foundation_app(output):
    # 测试进程独有额度；代码不加载.env，不构造任何真实供应商模型。
    os.environ["TAYUNE_DAILY_REQUEST_LIMIT"] = "10000"
    runtime = FoundationRuntime(output)
    app = create_app(runtime_factory=lambda: runtime)
    app.state.request_limiter.limits = {"/api/session": 100000, "/api/chat": 100000}

    @app.get("/benchmark/stats")
    async def stats():
        return {**app.state.service.requests.snapshot(), "dependencies": "all simulated",
                "cpu_peak_pending": runtime.cpu.peak_pending,
                "classifier_peak_pending": runtime.classifier_cpu.peak_pending,
                "provider_attempts": runtime.provider_attempts,
                "task_statuses": dict(runtime.sessions.db.execute("SELECT status,COUNT(*) FROM task_runs GROUP BY status").fetchall()),
                "daily_count": runtime.sessions.db.execute("SELECT COALESCE(SUM(count),0) FROM budget").fetchone()[0],
                "sessions": runtime.sessions.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]}

    @app.post("/benchmark/export_tokens")
    async def export_tokens():
        # 仅导出经正式HTTP创建的测试会话，不生成或预置任何额外会话。
        tokens = [row[0] for row in runtime.sessions.db.execute("SELECT token FROM sessions").fetchall()]
        path = output / "registered_tokens.json"
        path.write_text(json.dumps(tokens), encoding="utf-8")
        path.chmod(0o600)
        return {"registered_sessions": len(tokens), "seeded": False}

    return app


async def eight_chats(tokens):
    async with httpx.AsyncClient(trust_env=False, timeout=15) as client:
        async def ask(index):
            question = "Show one oolong under 30 USD." if index % 2 == 0 else "What is your return policy?"
            response = await client.post(HOST + "/api/chat", headers={"Authorization": "Bearer " + tokens[index]},
                json={"message": question, "request_id": f"capacity-foundation-{index:04d}"})
            body = response.json()
            return {"status": response.status_code, "degraded": body.get("degraded"),
                    "task_id": body.get("task_id"), "question": question}
        return await asyncio.gather(*(ask(index) for index in range(8)))


def execute(args):
    output = args.output.resolve()
    output.mkdir(parents=True)
    settings = {"TAYUNE_MAX_SESSIONS": "5000", "TAYUNE_MAX_CONCURRENT_CHATS": "4",
                "TAYUNE_MAX_WAITING_CHATS": "16", "TAYUNE_ADMISSION_WAIT_SECONDS": "2"}
    manifest = {"commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                "dependencies": "all simulated", "settings": settings, "paid_calls": 0,
                "seeded_sessions": 0, "loadgen": "separate process, same machine",
                "code_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                                for folder in (ROOT / "app", ROOT / "tests/performance") for path in folder.rglob("*.py")
                                if not path.stem.endswith("_1")}}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2))
    checks, stages = [], []
    with patch.dict(os.environ, settings), server(output / "server", module="tests.performance.verify_capacity_foundation",
                                                extra_args=["--serve"]) as backend:
        with httpx.Client(trust_env=False, timeout=15) as client:
            tokens, statuses = [], []
            started = time.perf_counter()
            for _ in range(3000):
                response = client.post(HOST + "/api/session", json={})
                statuses.append(response.status_code)
                if response.status_code == 200:
                    tokens.append(response.json()["session_token"])
            registration = {"attempts": len(statuses), "created": len(tokens), "rejected": statuses.count(429),
                            "seconds": time.perf_counter() - started, "seeded": False}
            (output / "registration.json").write_text(json.dumps(registration, indent=2))
            checks.append({"case": "formal3000_registration", "pass": len(tokens) == 3000 and len(set(tokens)) == 3000})
            if not checks[-1]["pass"]:
                raise RuntimeError("Registration failed; do not conceal it with seeded tokens")
            rows = asyncio.run(eight_chats(tokens))
            stats = client.get(HOST + "/benchmark/stats").json()
            checks += [{"case": "eight_complete_with_queue", "pass": all(row["status"] == 200 and row["degraded"] is False for row in rows)},
                       {"case": "four_active_not_eight", "pass": stats["peak_active"] == 4},
                       {"case": "waiting_observed", "pass": stats["peak_waiting"] >= 4},
                       {"case": "quota_only_eight", "pass": stats["daily_count"] == 8},
                       {"case": "unique_task_ids", "pass": len({row["task_id"] for row in rows}) == 8},
                       {"case": "released", "pass": stats["active"] == stats["waiting"] == stats["owners"] == 0}]
            attempts = stats["provider_attempts"]
            replay = client.post(HOST + "/api/chat", headers={"Authorization": "Bearer " + tokens[0]},
                json={"message": rows[0]["question"], "request_id": "capacity-foundation-0000"})
            checks.append({"case": "replay_no_extra_calls", "pass": replay.status_code == 200
                and client.get(HOST + "/benchmark/stats").json()["provider_attempts"] == attempts})
            (output / "contracts.json").write_text(json.dumps({"checks": checks, "stats": stats, "rows": rows}, indent=2))
            print("CONTRACTS", json.dumps({"passed": sum(row["pass"] for row in checks), "n": len(checks),
                                            "registration": registration, "stats": stats}), flush=True)
            if not all(row["pass"] for row in checks):
                raise RuntimeError("Foundation contracts failed")
            if args.load_python:
                client.post(HOST + "/benchmark/export_tokens").raise_for_status()
                time.sleep(8.2)
                for spike, seconds in ((False, 60), (True, 25)):
                    item = run_stage(output, 3000, seconds, spike, backend, args.load_python.absolute(),
                                     output / "server/registered_tokens.json")
                    stages.append(item)
                    print("STAGE", json.dumps(item), flush=True)
                    if item["stop_reason"] or item["transport_failures"] or item["http_5xx"]:
                        break
                    time.sleep(8.2)
            final = client.get(HOST + "/benchmark/stats").json()
            (output / "summary.json").write_text(json.dumps({"registration": registration, "checks": checks,
                                                            "stages": stages, "final": final}, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", choices=["diagnostic"], default="diagnostic")
    parser.add_argument("--load-python", type=Path)
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / "reports") or output.exists():
        parser.error("Output must be a new reports directory")
    if args.serve:
        import uvicorn
        sock = socket.socket()
        sock.bind(("127.0.0.1", 8012))
        try:
            asyncio.run(uvicorn.Server(uvicorn.Config(foundation_app(output), access_log=False,
                         log_level="warning", proxy_headers=False, backlog=4096)).serve(sockets=[sock]))
        finally:
            sock.close()
    else:
        execute(args)


if __name__ == "__main__":
    main()
