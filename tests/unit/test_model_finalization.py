"""Offline physical-budget finalization, including SDK wire-format verification."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI

from app.agent.graph import build_graph
from app.core.execution import CURRENT_RUN, Execution
from app.database.runs import RunStore
from app.database.session import SessionStore
from app.integrations.deepseek_tools import ToolModel
from app.integrations.model_gateway import ModelGateway, ModelUnavailable


class RecordingModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def bind_tools(self, tools):
        return BoundModel(self, tools)

    def bind(self, **options):
        return BoundModel(self, None, options)

    async def ainvoke(self, messages):
        return await BoundModel(self, None).ainvoke(messages)


class BoundModel:
    def __init__(self, root, tools, options=None):
        self.root, self.tools, self.options = root, tools, options or {}

    def bind(self, **options):
        return BoundModel(self.root, self.tools, {**self.options, **options})

    async def ainvoke(self, messages):
        self.root.requests.append({"messages": list(messages), "tools": self.tools, "options": self.options})
        response = self.root.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def reply(content="<reply:unavailable>", calls=None):
    return AIMessage(content=content, tool_calls=calls or [],
        response_metadata={"finish_reason": "tool_calls" if calls else "stop"},
        usage_metadata={"input_tokens": 4, "output_tokens": 2, "total_tokens": 6})


class FinalizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.sessions = SessionStore(Path(self.directory.name) / "fixture.sqlite3")
        self.runs = RunStore(self.sessions)
        self.run_id = self.runs.begin(self.sessions.create(), "finalization-case-0001", "fixture", {}, 20)
        self.execution = Execution(self.run_id, self.runs, {})
        self.marker = CURRENT_RUN.set(self.execution)

    async def asyncTearDown(self):
        CURRENT_RUN.reset(self.marker)
        self.sessions.close()
        self.directory.cleanup()

    async def invoke(self, model, role="main", options=None):
        gateway = ModelGateway({role: model}, {role: "fixture"})
        return await gateway.invoke(role, [HumanMessage(content="fixture")], tools=["fixture-tool"], options=options)

    async def test_last_physical_call_is_final_even_when_graph_count_is_lower(self):
        self.execution.model_calls = 3
        model = RecordingModel([reply()])
        await self.invoke(model)
        self.assertEqual(model.requests[0]["options"]["tool_choice"], "none")
        self.assertIn("final answer opportunity", model.requests[0]["messages"][-1].content)
        self.assertEqual(self.execution.model_calls, 4)

    async def test_earlier_call_still_allows_tool_choice(self):
        model = RecordingModel([reply()])
        await self.invoke(model)
        self.assertNotIn("tool_choice", model.requests[0]["options"])
        self.assertEqual(len(model.requests[0]["messages"]), 1)

    async def test_exhausted_tools_trigger_finalization_before_last_model_slot(self):
        self.execution.tool_attempts = 4
        model = RecordingModel([reply()])
        await self.invoke(model)
        self.assertEqual(model.requests[0]["options"]["tool_choice"], "none")
        self.assertEqual(self.execution.model_calls, 1)

    async def test_retry_recalculates_remaining_physical_budget(self):
        self.execution.model_calls = 2
        model = RecordingModel([httpx.ConnectError("fixture"), reply()])
        await self.invoke(model)
        self.assertNotIn("tool_choice", model.requests[0]["options"])
        self.assertEqual(model.requests[1]["options"]["tool_choice"], "none")
        self.assertEqual(self.execution.model_calls, 4)

    async def test_finalization_overrides_auto_without_mutating_caller_options(self):
        self.execution.model_calls = 3
        options = {"tool_choice": "auto", "max_tokens": 80}
        model = RecordingModel([reply()])
        await self.invoke(model, options=options)
        self.assertEqual(model.requests[0]["options"], {"tool_choice": "none", "max_tokens": 80})
        self.assertEqual(options["tool_choice"], "auto")

    async def test_provider_ignoring_none_is_rejected_and_usage_retained(self):
        self.execution.model_calls = 3
        model = RecordingModel([reply("", [{"name": "search_products", "args": {}, "id": "bad"}])])
        with self.assertRaisesRegex(ModelUnavailable, "ignored_finalization"):
            await self.invoke(model)
        self.assertEqual(self.execution.input_tokens, 4)
        self.assertEqual(self.execution.tool_attempts, 0)
        self.assertEqual(len(model.requests), 1)

    async def test_sdk_sends_tool_choice_none_on_the_wire(self):
        bodies = []

        def handler(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "fixture", "object": "chat.completion", "created": 1,
                "model": "deepseek-chat", "choices": [{"index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "<reply:unavailable>"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}})

        self.execution.model_calls = 3
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            llm = ChatOpenAI(model="deepseek-chat", api_key="fixture-not-a-secret",
                base_url="https://fixture.invalid/v1", max_retries=0, http_async_client=client,
                use_responses_api=False)
            gateway = ModelGateway({"main": llm}, {"main": "deepseek-chat"})
            tools = [{"type": "function", "function": {"name": "search_products", "parameters": {"type": "object"}}}]
            await ToolModel(gateway.role("main"), tools).ask([HumanMessage(content="fixture")])
        self.assertEqual(len(bodies), 1)
        self.assertEqual(bodies[0]["tool_choice"], "none")
        self.assertIn("final answer opportunity", bodies[0]["messages"][-1]["content"])

    async def test_graph_finalizes_after_four_reads_with_review_already_counted(self):
        class Catalog:
            name = "search_products"

            async def ainvoke(self, args):
                return {"ok": True, "data": {"products": [{"id": 7, "name": "Fixture Jasmine",
                    "url": "https://tayunetea.com/product/fixture/", "currency": "USD", "price": "19.90"}]}}

        def calls(prefix):
            return [{"id": prefix + str(index), "name": "search_products", "args": {"search": str(index)}} for index in range(2)]

        model = RecordingModel([reply("", calls("first")), reply("", calls("second")),
            reply('{"selections":[{"source_id":"S1","evidence_id":"E1"}]}')])
        self.execution.model_calls = 1
        gateway = ModelGateway({"main": model}, {"main": "fixture"})
        tools = [Catalog()]
        with patch("app.agent.graph.setup_logging"):
            graph = build_graph(ToolModel(gateway.role("main"), tools), tools)
        result = await graph.ainvoke({"messages": [HumanMessage(content="fixture")], "sources": []})
        self.assertFalse(result["degraded"])
        self.assertIn("USD 19.90", result["answer"])
        self.assertEqual(self.execution.model_calls, 4)
        self.assertEqual(self.execution.tool_attempts, 4)
        self.assertEqual(model.requests[-1]["options"]["tool_choice"], "none")


if __name__ == "__main__":
    unittest.main()
