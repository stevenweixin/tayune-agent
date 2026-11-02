"""仅监听8012的模拟依赖服务，运行正式API、Service、Graph、网关及SQLite。

不连接DeepSeek、Milvus或商品网站。此处的模型、分类和知识数据是固定样例，
因此只能测执行成本/保护行为，不能推断真实模型准确率或供应商吞吐。
"""
import argparse
import asyncio
import json
import time
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage

from app.agent.graph import build_graph
from app.agent.product_tool import build_product_tools
from app.agent.service import TeaService
from app.core.cpu_worker import CPUWorker
from app.core.logger import setup_logging
from app.database.runs import RunStore
from app.database.session import SessionStore
from app.integrations.deepseek_tools import ToolModel
from app.integrations.model_gateway import ModelGateway
from app.main import ROOT, create_app
from tests.evaluation.run_tasks import FixtureCatalog, fixture_knowledge


class ProviderError(RuntimeError):
    status_code = 401


class SimulatedModel:
    def __init__(self, runtime, options=None):
        self.runtime = runtime
        self.options = options or {}

    def bind_tools(self, tools):
        return self

    def bind(self, **options):
        return SimulatedModel(self.runtime, {**self.options, **options})

    async def ainvoke(self, messages):
        self.runtime.provider_attempts += 1
        await asyncio.sleep(self.runtime.model_delay)
        if self.runtime.fault == "model_auth":
            raise ProviderError("injected authentication failure")
        if self.runtime.fault == "model_transport":
            import httpx
            raise httpx.ConnectError("injected transport failure")
        usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        metadata = {"finish_reason": "stop", "model_name": "simulated-not-deepseek"}
        if self.runtime.fault == "invalid_json":
            return AIMessage(content="not valid grounded JSON", usage_metadata=usage, response_metadata=metadata)
        if isinstance(messages[-1], ToolMessage):
            tool_result = json.loads(messages[-1].content)
            content = '{"selections":[{"source_id":"S1","evidence_id":"E1"},{"source_id":"S1","evidence_id":"E2"}]}'
            if not tool_result.get("ok"):
                content = "<reply:unavailable>"
            return AIMessage(content=content, usage_metadata=usage, response_metadata=metadata)
        name = "search_products"
        args = {"search": "oolong", "max_price_usd": "30", "product_scope": "tea"}
        if self.runtime.fault == "guessed_id":
            name, args = "get_product", {"product_id": 999}
        if self.runtime.fault == "unknown_tool":
            name, args = "delete_order", {}
        metadata["finish_reason"] = "tool_calls"
        return AIMessage(content="", tool_calls=[{"id": "fixture-call", "name": name, "args": args}],
                         usage_metadata=usage, response_metadata=metadata)


class DelayedCatalog(FixtureCatalog):
    def __init__(self, runtime):
        super().__init__()
        self.runtime = runtime

    async def search_products(self, *args, **kwargs):
        await asyncio.sleep(self.runtime.tool_delay)
        if self.runtime.fault == "tool_down":
            raise RuntimeError("injected tool outage")
        return await super().search_products(*args, **kwargs)


class OfflineRuntime:
    def __init__(self, output, model_delay=0.25, tool_delay=0.05):
        self.output = output
        self.model_delay = model_delay
        self.tool_delay = tool_delay
        self.provider_attempts = 0
        self.fault = "none"
        self.cpu = CPUWorker()
        self.versions = {"models": "simulated", "knowledge": "fixture", "code": "engineering-baseline-v1"}

    async def start(self):
        self.output.mkdir(parents=True, exist_ok=False)
        setup_logging(self.output / "backend.log")
        self.sessions = SessionStore(self.output / "sessions.sqlite3")
        self.runs = RunStore(self.sessions)
        self.sessions.runs = self.runs
        self.classifier = SimpleNamespace(classify=lambda text: {"label": "in_scope", "score": 1.0})
        self.milvus = SimpleNamespace(has_collection=lambda *args: True)
        model = SimulatedModel(self)
        self.gateway = ModelGateway({"main": model, "summary": model}, {"main": "simulated", "summary": "simulated"})
        tools = build_product_tools(DelayedCatalog(self))
        tools.append(fixture_knowledge("knowledge"))
        self.graph = build_graph(ToolModel(self.gateway.role("main"), tools), tools)

    async def close(self):
        await self.cpu.close()
        self.sessions.close()


class ObservedService(TeaService):
    def __init__(self, runtime, daily_limit, max_concurrent=1):
        # 旧模拟基线显式保留单准入；正式入口不使用这个测试类。
        super().__init__(runtime, daily_limit, max_concurrent=max_concurrent)
        self.outcomes = Counter()

    async def chat(self, *args, **kwargs):
        started = time.perf_counter()
        try:
            result = await super().chat(*args, **kwargs)
        except (RuntimeError, ValueError, LookupError) as error:
            self.outcomes[str(error)] += 1
            raise
        self.outcomes["degraded" if result["degraded"] else "completed"] += 1
        with (self.runtime.output / "service.jsonl").open("a", encoding="utf-8") as file:
            file.write(json.dumps({"elapsed_seconds": time.perf_counter() - started,
                                   "degraded": result["degraded"], "task_id": result.get("task_id")}) + "\n")
        return result


def benchmark_app(output, profile="diagnostic", model_delay=0.25, tool_delay=0.05):
    runtime = OfflineRuntime(output, model_delay, tool_delay)
    api = create_app(runtime_factory=lambda: runtime)
    original_lifespan = api.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        async with original_lifespan(app):
            # 测试实例自己的额度和入口限制，生产8011进程完全不变。
            daily_limit = app.state.service.daily_limit if profile == "production" else 10000
            app.state.service = ObservedService(runtime, daily_limit)
            if profile == "diagnostic":
                app.state.request_limiter.limits = {"/api/session": 10000, "/api/chat": 10000}
            yield

    api.router.lifespan_context = lifespan

    @api.get("/benchmark/stats")
    async def stats():
        return {"profile": profile, "dependencies": "simulated", "provider_attempts": runtime.provider_attempts,
                "model_delay_seconds": model_delay, "tool_delay_seconds": tool_delay,
                "outcomes": dict(api.state.service.outcomes)}

    @api.post("/benchmark/fault/{name}")
    async def fault(name: str):
        if name not in {"none", "model_auth", "model_transport", "invalid_json", "tool_down", "unknown_tool", "guessed_id"}:
            from fastapi import HTTPException
            raise HTTPException(400, "Unknown fixture fault")
        runtime.fault = name
        runtime.gateway.failures.clear()
        runtime.gateway.open_until.clear()
        return {"fault": name}

    return api


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", choices=["production", "diagnostic"], default="diagnostic")
    parser.add_argument("--model-delay", type=float, default=0.25)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to((ROOT / "reports").resolve()) or output.exists():
        parser.error("Output must be a new directory below project reports/")
    if not 0 <= args.model_delay <= 5:
        parser.error("model-delay must be between 0 and 5 seconds")
    import uvicorn
    uvicorn.run(benchmark_app(output, args.profile, args.model_delay), host="127.0.0.1", port=8012,
                workers=1, access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()
