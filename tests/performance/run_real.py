"""隔离真实依赖评测。需明确--paid，最多3条冒烟+60条/120次模型/估算US$1。

读取生产配置和知识库，但不打开生产会话DB，不入库，不修改网站、环境或额度。
结果是英文合成探针的契约检查，不能叫人工答案正确率或线上SLA。
"""
import argparse
import asyncio
import hashlib
import json
import os
import time
import uuid
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlparse

import httpx
import psutil
import uvicorn

from app.core.logger import setup_logging
from app.core.runtime import ROOT, Runtime
from app.main import create_app
from tests.performance.budget import BudgetModel, EvaluationBudget
from tests.performance.cases import evaluation_cases
from tests.performance.run_offline import HOST, distribution


class EvaluationRuntime(Runtime):
    def __init__(self, output, budget):
        super().__init__(session_path=output / "sessions.sqlite3")
        self.budget = budget

    async def start(self):
        await super().start()
        for role, model in list(self.gateway.routes.items()):
            self.gateway.routes[role] = BudgetModel(model, self.budget, role)
        # 不启用额外备用供应商/模型；避免预算范围以外的实际调用。
        self.gateway.fallback = None


def contract(case, result, audit, checkpoint):
    """机器规则只检查协议、来源及安全边界；语义覆盖需人工逐题复核。"""
    answer = result.get("answer", "")
    urls = [item.get("url") for item in result.get("sources", []) if item.get("url")]
    safe_urls = all(urlparse(url).scheme == "https" and urlparse(url).netloc == "tayunetea.com" for url in urls)
    tools = [event for event in audit if event["kind"] == "tool_execute"]
    category = case["category"]
    checks = {"nonempty_answer": bool(answer.strip()), "source_domain": safe_urls,
              "within_model_budget": result.get("model_calls", 0) <= 4,
              "within_tool_budget": result.get("tool_count", 0) <= 4}
    if category in {"product", "knowledge", "multi_turn", "smoke"}:
        checks["not_degraded"] = result.get("degraded") is False
        checks["has_evidence"] = bool(result.get("sources"))
    elif category == "restricted":
        # 成功拒答不要求来源；固定安全回复和无工具执行分别检查。
        checks["refusal"] = any(text in answer.lower() for text in ("cannot", "can't", "can only", "unable"))
        checks["no_business_tool_execution"] = not tools
    elif category == "out_of_scope":
        checks["no_business_tool_execution"] = not tools
        checks["redirects_to_scope"] = "tea" in answer.lower()
    return {"checks": checks, "contract_pass": all(checks.values()), "human_semantic_review": "pending"}


async def execute(args):
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / "reports"):
        raise ValueError("Output must be below reports/")
    output.mkdir(parents=True, exist_ok=False)
    setup_logging(output / "backend.log")
    # 只影响该子进程；不编辑.env，也不影响已经运行的8011。
    for key in ("DEEPSEEK_MODEL", "TAYUNE_MAIN_MODEL", "TAYUNE_REVIEW_MODEL", "TAYUNE_SUMMARY_MODEL"):
        os.environ[key] = "deepseek-flash"
    os.environ["TAYUNE_FALLBACK_MODEL"] = ""
    os.environ["TAYUNE_DAILY_REQUEST_LIMIT"] = "100"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    cases = evaluation_cases()
    frozen = "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases)
    (output / "frozen_cases.jsonl").write_text(frozen, encoding="utf-8")
    budget = EvaluationBudget(output / "model_attempts.jsonl")
    previous_chats = 0
    if args.previous:
        previous = args.previous.resolve()
        if not previous.is_relative_to(ROOT / "reports"):
            raise ValueError("Previous evidence must be below reports/")
        previous_manifest = json.loads((previous / "manifest.json").read_text())
        if previous_manifest["dataset_sha256"] != hashlib.sha256(frozen.encode()).hexdigest():
            raise ValueError("Cannot change the frozen dataset between attempts")
        previous_summary = json.loads((previous / "summary.json").read_text())
        if previous_summary["stop_reason"] == "unknown_provider_usage":
            raise ValueError("Unknown usage requires reconciliation, not a restarted budget")
        budget.attempts = previous_summary["model_attempts"]
        budget.estimated_usd = previous_summary["estimated_usd"]
        previous_chats = previous_summary["completed_chat_requests"]
    runtime = EvaluationRuntime(output, budget)
    app = create_app(runtime_factory=lambda: runtime)
    # 放开本测试进程的IP入口计数；正式服务不变，Service的锁/8秒/任务预算保留。
    app.state.request_limiter.limits = {"/api/session": 10000, "/api/chat": 10000}
    manifest = {"dataset_sha256": hashlib.sha256(frozen.encode()).hexdigest(), "planned_cases": len(cases),
                "limits": {"chat_requests": 63, "model_attempts": 120, "estimated_usd": 1},
                "price_basis": "conservative peak cache-miss input $0.30/M, output $1.20/M; not a bill",
                "prices_source": "https://api-docs.deepseek.com/quick_start/pricing/",
                "configured_model": "deepseek-flash", "production_model_unchanged": True,
                "previous_run": args.previous.name if args.previous else None,
                "previous_chats": previous_chats, "previous_model_attempts": budget.attempts,
                "dependencies": {name: version(name) for name in ("langgraph", "langchain-openai", "torch", "pymilvus")},
                "scope": "local HTTP, real CPU models/Milvus reads/WooCommerce public reads/DeepSeek; no Cloudflare",
                "dataset_kind": "synthetic English engineering probes, not independent human gold labels"}
    config = uvicorn.Config(app, host="127.0.0.1", port=8012, log_level="warning", access_log=False, proxy_headers=False)
    server = uvicorn.Server(config)
    started = time.perf_counter()
    # 使用自己的socket，端口占用直接失败，不接管或关闭别人的进程。
    import socket
    sock = socket.socket()
    sock.bind(("127.0.0.1", 8012))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    rows = []
    try:
        while not server.started:
            if task.done():
                await task
                raise RuntimeError("Evaluation server failed to start")
            if time.perf_counter() - started > 180:
                raise TimeoutError("Real dependency startup exceeded 180s")
            await asyncio.sleep(.2)
        manifest.update(startup_seconds=time.perf_counter() - started, versions=runtime.versions,
                        torch_threads=__import__("torch").get_num_threads(), rss_mb=psutil.Process().memory_info().rss / 1024 ** 2)
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2))
        print("Real dependencies ready; starting authorized smoke", flush=True)
        async with httpx.AsyncClient(trust_env=False, timeout=100) as client:
            # 三条冒烟也是完整HTTP请求；失败后停止，不堆量撞服务。
            smoke = [{"id": "smoke-" + str(index), "question": question, "category": "smoke", "session_group": None}
                     for index, question in enumerate(("Show me one oolong tea under 30 USD.",
                         "How should I brew oolong in a mug?", "What is your return policy?"), 1)]
            sessions = {}
            last_request = {}
            smoke_failures = 0
            for index, case in enumerate((smoke + cases)[previous_chats:], start=previous_chats):
                if budget.stopped or previous_chats + len(rows) >= 63:
                    break
                group = case["session_group"] or case["id"]
                if group not in sessions:
                    response = await client.post(HOST + "/api/session", json={})
                    response.raise_for_status()
                    sessions[group] = response.json()["session_token"]
                if group in last_request:
                    await asyncio.sleep(max(0, 8.2 - (time.monotonic() - last_request[group])))
                bearer = sessions[group]
                before = budget.attempts
                started_request = time.perf_counter()
                first_delta = None
                body = {}
                status = None
                last_request[group] = time.monotonic()
                if index < 3:
                    async with client.stream("POST", HOST + "/api/chat",
                        headers={"Authorization": "Bearer " + bearer, "Accept": "text/event-stream"},
                        json={"message": case["question"], "request_id": uuid.uuid4().hex}) as response:
                        status = response.status_code
                        event = None
                        async for line in response.aiter_lines():
                            if line.startswith("event:"):
                                event = line[6:].strip()
                            elif line.startswith("data:"):
                                data = json.loads(line[5:].strip())
                                if event == "delta" and first_delta is None:
                                    first_delta = (time.perf_counter() - started_request) * 1000
                                if event == "done":
                                    # SSE尾帧只含request_id/degraded；从本测试DB取已经完成的结果，
                                    # 不是再次发送聊天，避免把验收动作变成额外模型调用。
                                    saved_result = runtime.runs.get(bearer, data["request_id"])
                                    body = {**saved_result["result"], "request_id": data["request_id"]}
                else:
                    response = await client.post(HOST + "/api/chat", headers={"Authorization": "Bearer " + bearer},
                        json={"message": case["question"], "request_id": uuid.uuid4().hex})
                    status, body = response.status_code, response.json()
                elapsed = (time.perf_counter() - started_request) * 1000
                audit, saved = [], {}
                if body.get("task_id"):
                    audit = runtime.runs.audit(body["task_id"])
                    saved = runtime.runs.get(bearer, body["task_id"])
                row = {**case, "http_status": status, "elapsed_ms": elapsed, "first_delta_ms": first_delta,
                       "physical_model_attempts": budget.attempts - before, "result": body, "audit": audit,
                       "rss_mb": psutil.Process().memory_info().rss / 1024 ** 2}
                row.update(contract(case, body, audit, saved.get("checkpoint") or {}))
                rows.append(row)
                with (output / "results.jsonl").open("a", encoding="utf-8") as file:
                    file.write(json.dumps(row, ensure_ascii=False) + "\n")
                print(json.dumps({"case": case["id"], "status": status, "degraded": body.get("degraded"),
                                  "contract_pass": row["contract_pass"], "seconds": round(elapsed / 1000, 2),
                                  "model_attempts_total": budget.attempts, "estimated_usd": budget.estimated_usd,
                                  "stop": budget.stopped}), flush=True)
                if index < 3 and (status != 200 or body.get("degraded") or not body.get("sources")):
                    smoke_failures += 1
                    if smoke_failures >= 2:
                        budget.stopped = budget.stopped or "smoke_infrastructure_failed"
                        break
    finally:
        server.should_exit = True
        await task
        sock.close()
        summary = {"completed_chat_requests": len(rows) + previous_chats, "this_run_chat_requests": len(rows),
                   "model_attempts": budget.attempts,
                   "estimated_usd": budget.estimated_usd, "stop_reason": budget.stopped,
                   "http_200": sum(row["http_status"] == 200 for row in rows),
                   "contract_passes": sum(row["contract_pass"] for row in rows),
                   "degraded": sum(bool(row["result"].get("degraded")) for row in rows),
                   "latency": distribution([row["elapsed_ms"] for row in rows]),
                   "categories": {category: {"n": sum(row["category"] == category for row in rows),
                       "contract_passes": sum(row["category"] == category and row["contract_pass"] for row in rows)}
                       for category in {row["category"] for row in rows}},
                   "human_semantic_review": "pending; contract_pass is not answer correctness"}
        (output / "summary.json").write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--paid", action="store_true", help="Acknowledge real DeepSeek costs within the authorized cap")
    parser.add_argument("--previous", type=Path, help="Continue the SAME authorized budget after a known smoke failure")
    args = parser.parse_args()
    if not args.paid:
        parser.error("Real paid evaluation requires --paid and user authorization")
    asyncio.run(execute(args))


if __name__ == "__main__":
    main()
