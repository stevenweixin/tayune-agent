"""默认离线重放真实Graph/工具包装/门禁；live须显式付费许可，最多3题。"""
import argparse
import asyncio
import json
import tempfile
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

import httpx
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool

from app.agent.graph import build_graph
from app.agent.knowledge_tool import KnowledgeInput
from app.agent.product_tool import build_product_tools
from app.core.execution import CURRENT_RUN, Execution, fingerprint
from app.database.runs import RunStore
from app.database.session import SessionStore

ROOT = Path(__file__).resolve().parents[2]
CASES = Path(__file__).with_name("tea_tasks.json")


def request(name, args, number):
    return AIMessage(content="", tool_calls=[{"id": "call_" + str(number), "name": name, "args": args}])


class ReplayModel:
    def __init__(self, fixture):
        self.fixture = fixture
        self.count = 0

    async def ask(self, messages):
        self.count += 1
        mode = self.fixture["mode"]
        if mode == "short":
            return AIMessage(content="<reply:" + self.fixture["code"] + ">")
        if mode == "unsafe":
            return AIMessage(content="Use an unsupported 100 grams of tea.")
        if mode == "invented-source":
            return AIMessage(content='{"selections":[{"source_id":"S99","evidence_id":"E1"}]}')
        if mode == "loop":
            return request("search_products", {"search": "oolong"}, self.count)
        if self.count == 1:
            if mode in ("knowledge", "internal"):
                return request("search_knowledge", self.fixture["args"], self.count)
            if mode == "search":
                return request("search_products", self.fixture["args"], self.count)
            if mode == "detail":
                return request("search_products", {"search": "oolong"}, self.count)
            if mode == "parallel":
                return AIMessage(content="", tool_calls=[
                    {"id": "call_1a", "name": "search_products", "args": {"search": "oolong"}},
                    {"id": "call_1b", "name": "search_knowledge", "args": {"query": "oolong mug"}}])
            action = {
                "knowledge-failure": ("search_knowledge", {"query": "returns"}),
                "product-failure": ("search_products", {}),
                "guessed-id": ("get_product", {"product_id": 999}),
                "invalid-args": ("search_products", {"page": -1}),
                "unknown-tool": ("delete_order", {}),
            }[mode]
            return request(*action, self.count)
        if mode == "detail" and self.count == 2:
            return request("get_product", {"product_id": 7}, self.count)
        if mode in ("knowledge-failure", "product-failure", "guessed-id", "invalid-args", "unknown-tool"):
            return AIMessage(content="<reply:unavailable>")
        return AIMessage(content='{"selections":[{"source_id":"S1","evidence_id":"E1"}]}')


class FixtureCatalog:
    def __init__(self, fail=False):
        self.fail = fail

    async def search_products(self, search="", page=1, per_page=5, product_scope="tea"):
        if self.fail:
            raise RuntimeError("fixture_unavailable")
        scope_name = {"tea": "Fixture Oolong", "teaware": "Fixture Tea Pet", "chenpi": "Fixture Chenpi", "all": "Fixture Oolong"}[product_scope]
        return {"products": [{"id": 7, "name": scope_name, "url": "https://tayunetea.com/product/fixture/",
                              "price": "20.00", "currency": "USD", "is_in_stock": True}], "returned_count": 1}

    async def get_product(self, product_id):
        result = await self.search_products()
        return result["products"][0]


def fixture_knowledge(mode):
    async def lookup(query, source_type=None):
        if mode == "knowledge-failure":
            return {"ok": False, "error": "knowledge_unavailable"}
        return {"ok": True, "passages": [{"title": "Fixture guide", "text": "Fixture evidence only.",
                 "url": None if mode == "internal" else "https://tayunetea.com/fixture-guide/",
                 "chunk_id": "fixture-chunk", "evidence_key": "chunk:fixture-chunk"}], "top_score": 1.0}
    return StructuredTool.from_function(coroutine=lookup, name="search_knowledge", description="Fixture only", args_schema=KnowledgeInput)


def evaluate(case, result, events):
    calls = [event for event in events if event["kind"] == "tool_call"]
    names = [event["tool"] for event in calls]
    expected = case["expected"]
    checks = {"required_tools": set(expected["tools"]).issubset(names),
              "no_extra_tools": set(names).issubset(expected["tools"]),
              "budget": len(calls) <= expected.get("max_calls", 4),
              "degradation": result["degraded"] == expected["degraded"],
              "safe_links": all(source["url"].startswith("https://tayunetea.com/") for source in result.get("sources", []))}
    unauthorized = {event["step_id"] for event in calls if event.get("authorized") is False}
    checks["permission_boundary"] = not any(event.get("step_id") in unauthorized
        and (event["kind"] == "tool_execute" or event.get("status") == "succeeded") for event in events)
    if "args" in expected:
        checks["parameters"] = any(all(event.get("argument_hashes", {}).get(key) == fingerprint(value)
                                       for key, value in expected["args"].items()) for event in calls)
    if "error" in expected:
        checks["expected_error"] = any(event.get("error") == expected["error"] for event in events if event["kind"] == "tool_result")
    if "sources" in expected:
        checks["source_count"] = len(result.get("sources", [])) == expected["sources"]
    if "order" in expected:
        first, second = expected["order"]
        checks["dependency_order"] = first in names and second in names and names.index(first) < names.index(second)
    if expected.get("no_teaware"):
        checks["tea_scope"] = all(event.get("argument_hashes", {}).get("product_scope", fingerprint("tea")) == fingerprint("tea")
                                  for event in calls if event["tool"] == "search_products")
    return {"id": case["id"], "contract_pass": all(checks.values()), "checks": checks,
            "tool_calls": len(calls), "tools": names, "usage": result.get("usage"),
            "tool_executions": sum(event["kind"] == "tool_execute" for event in events),
            "model_calls": result.get("model_count"),
            "factual_support": "not_scored_requires_evidence_review"}


async def replay(case):
    with tempfile.TemporaryDirectory() as directory:
        sessions = SessionStore(Path(directory) / "fixture.sqlite3")
        runs = RunStore(sessions)
        token = sessions.create()
        run_id = runs.begin(token, "fixture-request-id", case["question"], {"knowledge": "fixture"}, 20)
        execution = Execution(run_id, runs, {"knowledge": "fixture"})
        marker = CURRENT_RUN.set(execution)
        started = perf_counter()
        try:
            tools = build_product_tools(FixtureCatalog(case["fixture"]["mode"] == "product-failure"))
            tools.append(fixture_knowledge(case["fixture"]["mode"]))
            messages = []
            for item in case.get("history", []):
                kind = HumanMessage if item["role"] == "human" else AIMessage
                messages.append(kind(content=item["text"]))
            messages.append(HumanMessage(content=case["question"]))
            with patch("app.agent.graph.setup_logging"):
                graph = build_graph(ReplayModel(case["fixture"]), tools)
            result = await graph.ainvoke({"messages": messages, "answer": "", "sources": [], "trace": [],
                      "known_product_ids": [], "model_count": 0, "tool_count": 0}, config={"recursion_limit": 12})
            evaluated = evaluate(case, result, runs.audit(run_id))
            evaluated["elapsed_seconds"] = round(perf_counter() - started, 4)
            return evaluated
        finally:
            CURRENT_RUN.reset(marker)
            sessions.close()


async def live(cases):
    results = []
    # 只使用现有loopback后端，不新建Runtime、不加载另一份模型/改写现有running状态。
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8011", trust_env=False, timeout=100) as client:
        for case in cases:
            response = await client.post("/api/session")
            response.raise_for_status()
            token = response.json()["session_token"]
            headers = {"Authorization": "Bearer " + token}
            response = await client.post("/api/chat", headers=headers, json={"message": case["question"]})
            response.raise_for_status()
            result = response.json()
            response = await client.get("/api/local/tasks/" + result["task_id"], headers=headers)
            response.raise_for_status()
            audit = response.json()
            evaluated = evaluate(case, result, audit["events"])
            evaluated["answer_for_manual_review"] = result["answer"]
            evaluated["versions"] = audit["versions"]
            results.append(evaluated)
            # 本任务的临时会话由评测自行删除；不触及用户会话或全站限额。
            await client.delete("/api/local/memory", headers=headers)
    return results


async def main():
    parser = argparse.ArgumentParser(description="英文业务轨迹评测；默认完全离线")
    parser.add_argument("--mode", choices=["replay", "live"], default="replay")
    parser.add_argument("--allow-paid", action="store_true")
    parser.add_argument("--max-cases", type=int)
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    dataset = json.loads(CASES.read_text(encoding="utf-8"))
    cases = dataset["cases"]
    if args.case_id:
        cases = [case for case in cases if case["id"] in args.case_id]
    if not cases:
        parser.error("没有匹配的case-id")
    if args.mode == "live" and (not args.allow_paid or not args.max_cases or not 1 <= args.max_cases <= 3):
        parser.error("live须显式--allow-paid与--max-cases 1到3；会消耗真实模型和每日额度")
    if args.mode == "live":
        # 故障注入/强制坏动作仅适用于重放，不能要求真实模型主动越权来满足测试轨迹。
        eligible = []
        for case in cases:
            mode = case["fixture"]["mode"]
            if mode in ("search", "knowledge", "detail", "parallel") and not case.get("history"):
                eligible.append(case)
        cases = eligible
        if not cases:
            parser.error("所选用例是离线故障注入或多轮上下文，不能直接作为单题live验收")
    if args.max_cases is not None and args.max_cases <= 0:
        parser.error("max-cases必须为正数")
    if args.max_cases:
        cases = cases[:args.max_cases]
    if args.mode == "live":
        results = await live(cases)
    else:
        results = []
        for case in cases:
            results.append(await replay(case))
    report = {"mode": args.mode, "dataset_version": dataset["version"], "case_count": len(results),
              "contract_passed": sum(item["contract_pass"] for item in results), "results": results,
              "limitation": "Replay validates execution contracts, NOT model selection accuracy, retrieval relevance or factual correctness."}
    if args.baseline:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        previous = {item["id"]: item["contract_pass"] for item in baseline["results"]}
        report["regressions"] = [item["id"] for item in results if previous.get(item["id"]) and not item["contract_pass"]]
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    if args.output:
        print(json.dumps({"mode": args.mode, "cases": len(results), "passed": report["contract_passed"],
                          "report": str(args.output), "failed_ids": [item["id"] for item in results if not item["contract_pass"]]}))
    else:
        print(rendered)
    return 0 if report["contract_passed"] == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
