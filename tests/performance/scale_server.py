"""3000用户诊断服务，仅8012；保留正式Service，依赖全部固定，不调用外部服务。"""
import argparse
import asyncio
import json
import os
import secrets
import socket
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from app.agent.graph import build_graph
from app.agent.knowledge_tool import build_knowledge_tool
from app.agent.product_tool import build_product_tools
from app.core.cpu_worker import CPUQueue
from app.integrations.deepseek_tools import ToolModel
from app.integrations.model_gateway import ModelGateway
from app.main import ROOT, create_app
from tests.performance.capacity_server import FixtureClassifier, FixtureRetriever, FixtureReranker, MixedModel
from tests.performance.run_offline import distribution
from tests.performance.server import OfflineRuntime, DelayedCatalog


class ScaleRuntime(OfflineRuntime):
    def __init__(self, output):
        super().__init__(output)
        self.cpu = CPUQueue()
        self.max_concurrent_chats = 4

    async def start(self):
        await super().start()
        self.classifier = FixtureClassifier()
        model = MixedModel(self)
        self.gateway = ModelGateway({"main": model, "summary": model}, {"main": "simulated", "summary": "simulated"})
        tools = build_product_tools(DelayedCatalog(self))
        tools.append(build_knowledge_tool(FixtureRetriever(), FixtureReranker(), self.cpu, max_pending=8))
        self.graph = build_graph(ToolModel(self.gateway.role("main"), tools), tools)


def scale_app(output):
    runtime = ScaleRuntime(output)
    app = create_app(runtime_factory=lambda: runtime)
    original = app.router.lifespan_context
    lag = deque(maxlen=600)
    lag_peak = 0

    async def heartbeat():
        nonlocal lag_peak
        while True:
            started = time.perf_counter()
            await asyncio.sleep(.1)
            elapsed = max(0, (time.perf_counter() - started - .1) * 1000)
            lag.append(elapsed)
            lag_peak = max(lag_peak, elapsed)

    @asynccontextmanager
    async def lifespan(api):
        # 只改隔离进程额度，正式4路、8秒及100会话上限没有修改。
        os.environ["TAYUNE_DAILY_REQUEST_LIMIT"] = "10000"
        async with original(api):
            api.state.request_limiter.limits = {"/api/session": 100000, "/api/chat": 100000}
            task = asyncio.create_task(heartbeat())
            try:
                yield
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = lifespan

    @app.post("/benchmark/seed")
    async def seed():
        # 明确绕过create()的100会话业务上限，只为单独测聊天入口；不是正式注册成功。
        tokens = [row[0] for row in runtime.sessions.db.execute("SELECT token FROM sessions").fetchall()]
        while len(tokens) < 3000:
            token = secrets.token_urlsafe(32)
            runtime.sessions.save(token, {"summary": "", "history": [], "known_product_ids": [],
                                          "last_request": 0, "preferences": {}})
            tokens.append(token)
        path = output / "synthetic_tokens.json"
        path.write_text(json.dumps(tokens), encoding="utf-8")
        path.chmod(0o600)
        return {"seeded_sessions": len(tokens), "formal_create_limit_bypassed": True}

    @app.get("/benchmark/stats")
    async def stats():
        service = app.state.service
        statuses = dict(runtime.sessions.db.execute("SELECT status,COUNT(*) FROM task_runs GROUP BY status").fetchall())
        return {"dependencies": "all simulated", "capacity": service.requests.capacity,
                "active": service.requests.active, "owners": len(service.requests.owners),
                "peak_active": service.requests.peak, "cpu_peak_pending": runtime.cpu.peak_pending,
                "task_statuses": statuses, "provider_attempts": runtime.provider_attempts,
                "daily_count": runtime.sessions.db.execute("SELECT COALESCE(SUM(count),0) FROM budget").fetchone()[0],
                "sessions": runtime.sessions.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
                "event_loop_lag_recent": distribution(list(lag)), "event_loop_lag_peak_ms": lag_peak}

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", choices=["diagnostic"], default="diagnostic")
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / "reports") or output.exists():
        parser.error("Output must be a new reports directory")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 8012))
    import uvicorn
    try:
        asyncio.run(uvicorn.Server(uvicorn.Config(scale_app(output), access_log=False,
                    log_level="warning", proxy_headers=False, backlog=4096)).serve(sockets=[sock]))
    finally:
        sock.close()


if __name__ == "__main__":
    main()
