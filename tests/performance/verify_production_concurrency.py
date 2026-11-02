"""正式Service/CPUQueue联调；可读取真实RAG，但所有LLM角色与商品接口均为固定样例。"""
import argparse
import asyncio
import json
import time
from pathlib import Path

import httpx

from app.agent.graph import build_graph
from app.agent.knowledge_tool import build_knowledge_tool
from app.agent.product_tool import build_product_tools
from app.agent.service import TeaService
from app.core.cpu_worker import CPUQueue
from app.core.logger import setup_logging
from app.core.runtime import Runtime
from app.integrations.deepseek_tools import ToolModel
from app.main import create_app
from tests.performance.capacity_server import CapacityOfflineRuntime, MixedModel
from tests.performance.run_offline import ROOT
from tests.performance.server import DelayedCatalog


async def verify(output, real_retrieval):
    if output.exists() or not output.is_relative_to(ROOT / "reports"):
        raise ValueError("Output must be a new directory below reports/")
    captured = {}
    if real_retrieval:
        setup_logging(output / "backend.log")
        runtime = Runtime(session_path=output / "sessions.sqlite3")
        load_models = runtime.load_models

        def capture_models():
            models = load_models()
            captured["retriever"], captured["reranker"] = models[1:]
            return models

        runtime.load_models = capture_models
    else:
        runtime = CapacityOfflineRuntime(output, queued=True)
        runtime.cpu = CPUQueue()
    tasks = []
    try:
        await runtime.start()
        # 替换全部角色和备用路由后才允许发送任何测试问题，不能漏掉意图复核/摘要。
        runtime.provider_attempts, runtime.fault = 0, "none"
        runtime.model_delay, runtime.tool_delay = .25, .05
        model = MixedModel(runtime)
        for role in runtime.gateway.routes:
            runtime.gateway.routes[role] = model
            runtime.gateway.names[role] = "simulated-no-paid-provider"
        runtime.gateway.fallback = None
        runtime.versions = {**runtime.versions, "models": dict(runtime.gateway.names)}
        if real_retrieval:
            tools = build_product_tools(DelayedCatalog(runtime))
            tools.append(build_knowledge_tool(captured["retriever"], captured["reranker"], runtime.cpu, max_pending=8))
            runtime.graph = build_graph(ToolModel(runtime.gateway.role("main"), tools), tools)
        service = TeaService(runtime, max_concurrent=4)
        app = create_app()
        app.state.runtime, app.state.service = runtime, service
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1))
        checks, rows = [], []
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8012") as client:
            tokens = [runtime.sessions.create() for _ in range(5)]
            questions = ["Do you sell jasmine tea?", "What is your return policy?"] * 2

            async def ask(index):
                started = time.perf_counter()
                response = await client.post("/api/chat", headers={"Authorization": "Bearer " + tokens[index]},
                    json={"message": questions[index], "request_id": f"formal-concurrency-{index}"})
                return {"http": response.status_code, "elapsed_seconds": time.perf_counter() - started,
                        "result": response.json()}

            tasks = [asyncio.create_task(ask(index)) for index in range(4)]
            for _ in range(200):
                if service.requests.active == 4:
                    break
                await asyncio.sleep(.005)
            checks.append({"case": "four_inflight", "pass": service.requests.active == 4})
            extra = await client.post("/api/chat", headers={"Authorization": "Bearer " + tokens[4]},
                                      json={"message": "extra", "request_id": "rejected-capacity"})
            duplicate = await client.post("/api/chat", headers={"Authorization": "Bearer " + tokens[0]},
                                          json={"message": questions[0], "request_id": "formal-concurrency-0"})
            checks.append({"case": "capacity_and_same_owner_rejected", "pass": extra.status_code == duplicate.status_code == 429})
            own = await client.post("/api/local/memory", headers={"Authorization": "Bearer " + tokens[0]}, json={"vessel": "mug"})
            other = await client.post("/api/local/memory", headers={"Authorization": "Bearer " + tokens[4]}, json={"vessel": "mug"})
            checks.append({"case": "management_owner_isolation", "pass": own.status_code == 409 and other.status_code == 200})
            rows = await asyncio.gather(*tasks)
            checks.append({"case": "four_completed", "pass": all(row["http"] == 200 and row["result"].get("degraded") is False for row in rows)})
            checks.append({"case": "task_ids_unique", "pass": len({row["result"].get("task_id") for row in rows}) == 4})
            before = runtime.provider_attempts
            replay = await client.post("/api/chat", headers={"Authorization": "Bearer " + tokens[0]},
                                       json={"message": questions[0], "request_id": "formal-concurrency-0"})
            checks.append({"case": "replay_no_extra_model", "pass": replay.status_code == 200
                           and replay.json().get("task_id") == rows[0]["result"].get("task_id") and runtime.provider_attempts == before})
            for index in range(4):
                history = runtime.sessions.get(tokens[index])["history"]
                checks.append({"case": "history_" + str(index), "pass": len(history) == 2 and history[0]["text"] == questions[index]})
            count = runtime.sessions.db.execute("SELECT SUM(count) FROM budget").fetchone()[0]
            checks.append({"case": "quota_only_charges_admitted", "pass": count == 4})
            checks.append({"case": "slots_and_owners_released", "pass": service.requests.active == 0 and not service.requests.owners})
        result = {"real_local_models_and_milvus": real_retrieval, "llm": "simulated", "catalog": "fixture",
                  "checks": checks, "passed": sum(item["pass"] for item in checks), "total": len(checks),
                  "peak_active": service.requests.peak, "cpu_peak_pending": runtime.cpu.peak_pending,
                  "simulated_model_attempts": runtime.provider_attempts, "paid_model_attempts": 0, "rows": rows}
        (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({key: value for key, value in result.items() if key not in ("checks", "rows")}), flush=True)
        if result["passed"] != result["total"]:
            raise RuntimeError("Formal concurrency verification failed")
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runtime.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--real-retrieval", action="store_true", help="Read actual local models/Milvus; never call a paid LLM")
    args = parser.parse_args()
    asyncio.run(verify(args.output.resolve(), args.real_retrieval))


if __name__ == "__main__":
    main()
