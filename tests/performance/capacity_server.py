"""容量实验HTTP服务：仅loopback8012，独立DB，支持模拟或已授权的真实依赖。"""
import argparse
import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path

from langchain_core.documents import Document
from langchain_core.messages import HumanMessage, ToolMessage

from app.agent.graph import build_graph
from app.agent.knowledge_tool import build_knowledge_tool
from app.agent.product_tool import build_product_tools
from app.core.logger import setup_logging
from app.integrations.deepseek_tools import ToolModel
from app.integrations.model_gateway import ModelGateway
from app.main import ROOT, create_app
from tests.performance.budget import EvaluationBudget
from tests.performance.capacity_runtime import CapacityService, QueuedCPU
from tests.performance.run_offline import distribution
from tests.performance.run_real import EvaluationRuntime
from tests.performance.server import OfflineRuntime, SimulatedModel, DelayedCatalog


class SharedBudget(EvaluationBudget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reserved_usd = 0.0

    def reserve(self):
        # 为每个在途调用预留5美分，不能让多个调用同时花同一份剩余额度。
        if self.estimated_usd + self.reserved_usd + .05 > self.max_usd:
            self.stopped = "concurrent_cost_reservation_limit"
            raise RuntimeError("evaluation_budget_exhausted")
        number = super().reserve()
        self.reserved_usd += .05
        return number

    def finish(self, *args, **kwargs):
        super().finish(*args, **kwargs)
        self.reserved_usd = max(0, self.reserved_usd - .05)


class CapacityRealRuntime(EvaluationRuntime):
    def __init__(self, output, budget, queued):
        super().__init__(output, budget)
        self.output = output
        if queued:
            self.cpu = QueuedCPU()


class FixtureClassifier:
    def classify(self, question):
        time.sleep(.02)
        return {"label": "in_scope", "score": 1.0}


class FixtureRetriever:
    def search(self, query, **kwargs):
        return [(Document(page_content="Opened tea cannot be returned.\nContact the shop for damaged parcels.",
                          metadata={"title": "Fixture returns", "source_url": "https://tayunetea.com/fixture-returns/",
                                    "chunk_id": "fixture-policy", "language": "en"}), .8)]


class FixtureReranker:
    def rerank_with_confidence(self, query, documents, **kwargs):
        time.sleep(.2)
        return documents, .9


class MixedModel(SimulatedModel):
    def bind(self, **options):
        return MixedModel(self.runtime, {**self.options, **options})

    async def ainvoke(self, messages):
        response = await super().ainvoke(messages)
        if not isinstance(messages[-1], ToolMessage):
            question = next(item.content for item in reversed(messages) if isinstance(item, HumanMessage))
            if "return policy" in question.lower():
                response.tool_calls = [{"id": "fixture-call", "name": "search_knowledge",
                                        "args": {"query": "return policy", "source_type": "policy"}}]
        return response


class CapacityOfflineRuntime(OfflineRuntime):
    def __init__(self, output, queued):
        super().__init__(output)
        if queued:
            self.cpu = QueuedCPU()

    async def start(self):
        await super().start()
        self.classifier = FixtureClassifier()
        model = MixedModel(self)
        self.gateway = ModelGateway({"main": model, "summary": model}, {"main": "simulated", "summary": "simulated"})
        tools = build_product_tools(DelayedCatalog(self))
        tools.append(build_knowledge_tool(FixtureRetriever(), FixtureReranker(), self.cpu))
        self.graph = build_graph(ToolModel(self.gateway.role("main"), tools), tools)


def capacity_app(args):
    queued = args.profile == "queued"
    output = args.output.resolve()
    budget = None
    if args.dependencies == "real":
        if not args.paid or not 1 <= args.attempts <= 120 or not .05 <= args.usd <= 1:
            raise ValueError("Real experiments require paid acknowledgement and bounded budget")
        output.mkdir(parents=True, exist_ok=False)
        setup_logging(output / "backend.log")
        import os
        for key in ("DEEPSEEK_MODEL", "TAYUNE_MAIN_MODEL", "TAYUNE_REVIEW_MODEL", "TAYUNE_SUMMARY_MODEL"):
            os.environ[key] = "deepseek-flash"
        os.environ["TAYUNE_FALLBACK_MODEL"] = ""
        os.environ["TOKENIZERS_PARALLELISM"] = "false"
        budget = SharedBudget(output / "model_attempts.jsonl", max_attempts=args.attempts, max_usd=args.usd)
        runtime = CapacityRealRuntime(output, budget, queued)
    else:
        runtime = CapacityOfflineRuntime(output, queued)
    # 只改当前子进程模块变量；生产知识工具仍是最多2个待处理。
    import app.agent.knowledge_tool as knowledge
    knowledge.MAX_PENDING_KNOWLEDGE = max(2, args.capacity * 2) if queued else 2
    api = create_app(runtime_factory=lambda: runtime)
    original = api.router.lifespan_context
    lag = []

    async def heartbeat():
        while True:
            started = time.perf_counter()
            await asyncio.sleep(.1)
            lag.append(max(0, (time.perf_counter() - started - .1) * 1000))

    @asynccontextmanager
    async def lifespan(app):
        async with original(app):
            app.state.service = CapacityService(runtime, args.capacity)
            app.state.request_limiter.limits = {"/api/session": 10000, "/api/chat": 10000}
            heartbeat_task = asyncio.create_task(heartbeat())
            try:
                yield
            finally:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

    api.router.lifespan_context = lifespan

    @api.middleware("http")
    async def block_management_mutations(request, call_next):
        # 这是测试chat准入的原型，不声称已适配生产记忆编辑、删除、恢复等写入口。
        if request.url.path.startswith("/api/local/") and request.method != "GET":
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "Management mutations disabled in capacity experiment"}, status_code=403)
        return await call_next(request)

    @api.get("/benchmark/stats")
    async def stats():
        service = api.state.service
        budget_count = runtime.sessions.db.execute("SELECT COALESCE(SUM(count),0) FROM budget").fetchone()[0]
        runs = runtime.sessions.db.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0]
        info = {"dependencies": args.dependencies, "profile": args.profile, "capacity": args.capacity,
                "active": service.lock.active, "peak_active": service.lock.peak,
                "knowledge_max_pending": knowledge.MAX_PENDING_KNOWLEDGE, "outcomes": dict(service.outcomes),
                "budget_count": budget_count, "task_rows": runs, "event_loop_lag": distribution(lag),
                "cpu_max_pending": 16 if queued else 1, "versions": runtime.versions}
        if queued:
            info["cpu_peak_pending"] = runtime.cpu.peak_pending
            info["cpu_timings"] = {name: {"queue": distribution([item["queue_ms"] for item in items]),
                                          "run": distribution([item["run_ms"] for item in items])}
                                   for name, items in runtime.cpu.timings.items()}
        if budget:
            info["model_attempts"] = budget.attempts
            info["estimated_usd"] = budget.estimated_usd
            info["budget_stop"] = budget.stopped
        else:
            info["model_attempts"] = runtime.provider_attempts
            info["estimated_usd"] = 0
            info["budget_stop"] = None
        return info

    return api


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", choices=["original", "naive", "queued"], required=True)
    parser.add_argument("--capacity", type=int, choices=[1, 2, 4, 8], required=True)
    parser.add_argument("--dependencies", choices=["simulated", "real"], default="simulated")
    parser.add_argument("--paid", action="store_true")
    parser.add_argument("--attempts", type=int, default=120)
    parser.add_argument("--usd", type=float, default=1)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / "reports") or output.exists():
        parser.error("Output must be a new directory below reports/")
    if args.profile == "original" and args.capacity != 1:
        parser.error("Original profile must retain capacity 1")
    # 和离线入口相同，仅占用自己的8012；若已占用立即失败，不接管进程。
    import socket
    import uvicorn
    sock = socket.socket()
    sock.bind(("127.0.0.1", 8012))
    server = uvicorn.Server(uvicorn.Config(capacity_app(args), log_level="warning", access_log=False, proxy_headers=False))
    try:
        asyncio.run(server.serve(sockets=[sock]))
    finally:
        sock.close()


if __name__ == "__main__":
    main()
