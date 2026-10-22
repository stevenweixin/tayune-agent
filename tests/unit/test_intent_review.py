"""意图复核与服务预算回归；全部使用假模型，不调用付费API或真实会话库。"""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from app.agent.intent_review import IntentReviewer
from app.agent import service
from app.agent.graph import build_graph


class PlainModel:
    def __init__(self, response):
        self.response = response
        self.bound_parameters = None
        self.messages = []

    def bind(self, **kwargs):
        self.bound_parameters = kwargs
        return self

    async def ainvoke(self, messages):
        self.messages = list(messages)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def review_message(text, finish="stop", **kwargs):
    return AIMessage(content=text, response_metadata={"finish_reason": finish},
                     usage_metadata={"input_tokens": 5, "output_tokens": 1, "total_tokens": 6}, **kwargs)


class ReviewerTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_decisions_and_small_budget(self):
        for decision in ("PUBLIC", "RESTRICTED", "UNCLEAR"):
            model = PlainModel(review_message(decision))
            result = await IntentReviewer(model).review([HumanMessage(content="Tea?")])
            self.assertEqual(result["decision"], decision)
            self.assertEqual(result["usage"], {"input_tokens": 5, "output_tokens": 1})
            self.assertEqual(model.bound_parameters, {"max_tokens": 16, "temperature": 0})

    async def test_invalid_text_truncation_and_tool_request_are_unclear(self):
        responses = [review_message("PUBLIC because it is tea"), review_message(""),
                     review_message("PUBLIC", finish="length"),
                     review_message("PUBLIC", tool_calls=[{"name": "delete_order", "args": {}, "id": "x"}])]
        for response in responses:
            result = await IntentReviewer(PlainModel(response)).review([HumanMessage(content="Tea?")])
            self.assertEqual(result["decision"], "UNCLEAR")

    async def test_service_error_is_sanitized(self):
        with self.assertRaises(RuntimeError) as raised:
            await IntentReviewer(PlainModel(RuntimeError("private-secret"))).review([HumanMessage(content="Tea?")])
        self.assertNotIn("private-secret", str(raised.exception))

    async def test_timeout_does_not_retry(self):
        model = PlainModel(review_message("PUBLIC"))
        model.ainvoke = AsyncMock(side_effect=lambda messages: None)
        async def slow(messages):
            await asyncio.sleep(10)
        model.ainvoke.side_effect = slow
        original = asyncio.wait_for
        async def short_wait(awaitable, timeout):
            self.assertEqual(timeout, 15)
            return await original(awaitable, timeout=.01)
        with patch("app.agent.intent_review.asyncio.wait_for", short_wait):
            with self.assertRaises(RuntimeError):
                await IntentReviewer(model).review([HumanMessage(content="Tea?")])
        self.assertEqual(model.ainvoke.await_count, 1)

    async def test_recent_context_is_bounded(self):
        history = []
        for number in range(4):
            history.extend([HumanMessage(content=f"q{number}"), AIMessage(content=f"a{number}")])
        history.append(HumanMessage(content="Show me those tea pets."))
        model = PlainModel(review_message("PUBLIC"))
        await IntentReviewer(model).review(history)
        self.assertEqual(model.messages[-1].content, history[-1].content)
        self.assertEqual(len(model.messages), 4)  # System + 上一轮Human/AI + 本轮Human。
        self.assertNotIn("q0", [m.content for m in model.messages])


class FakeCPU:
    async def run(self, function, *args):
        return function(*args)


class FakeSessions:
    def __init__(self):
        self.data = {"summary": "", "history": [], "known_product_ids": [], "last_request": 0}
        self.count = 0

    def get(self, token):
        return deepcopy(self.data)

    def save(self, token, data):
        self.data = deepcopy(data)

    def reserve_request(self, limit):
        if self.count >= limit:
            return False
        self.count += 1
        return True


class ServiceReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime = SimpleNamespace(cpu=FakeCPU(), sessions=FakeSessions(),
            classifier=SimpleNamespace(classify=lambda question: {"label": "restricted", "score": .98}),
            intent_reviewer=SimpleNamespace(review=AsyncMock(return_value={"decision": "PUBLIC",
                "usage": {"input_tokens": 5, "output_tokens": 1}})), graph=SimpleNamespace(ainvoke=AsyncMock()))
        async def graph_result(state, config):
            result = dict(state)
            result.update(answer="A public product answer.", input_tokens=state["input_tokens"] + 7,
                          output_tokens=state["output_tokens"] + 2)
            return result
        self.runtime.graph.ainvoke.side_effect = graph_result
        async def compress(history, summary, **kwargs):
            return {"status": "unchanged", "summary": summary, "messages": list(history)}
        self.compress = AsyncMock(side_effect=compress)
        self.compress_patch = patch.object(service, "compress_history", self.compress)
        self.compress_patch.start()
        self.addCleanup(self.compress_patch.stop)

    async def test_public_reversal_reaches_graph_and_counts_review_usage(self):
        result = await service.TeaService(self.runtime).chat("token", "Show me your tea pets.")
        self.assertEqual(result["answer"], "A public product answer.")
        self.assertEqual(result["usage"], {"input_tokens": 12, "output_tokens": 3})
        state = self.runtime.graph.ainvoke.call_args.args[0]
        self.assertEqual(state["model_count"], 1)
        self.assertEqual(state["trace"], ["classifier:restricted", "intent_review:public"])
        self.assertEqual(self.runtime.sessions.count, 1)

    async def test_restricted_unclear_and_unknown_never_reach_graph(self):
        for decision in ("RESTRICTED", "UNCLEAR", "INVALID"):
            self.runtime.intent_reviewer.review.return_value = {"decision": decision,
                "usage": {"input_tokens": 5, "output_tokens": 1}}
            result = await service.TeaService(self.runtime).run(self.runtime.sessions.data, "token", "Sensitive request")
            self.assertEqual(result["sources"], [])
        self.runtime.graph.ainvoke.assert_not_awaited()
        self.compress.assert_not_awaited()

    async def test_review_outage_does_not_allow_tools(self):
        self.runtime.intent_reviewer.review.side_effect = RuntimeError("offline")
        result = await service.TeaService(self.runtime).chat("token", "Tea pets?")
        self.assertTrue(result["degraded"])
        self.runtime.graph.ainvoke.assert_not_awaited()
        self.compress.assert_not_awaited()

    async def test_normal_query_skips_review(self):
        self.runtime.classifier.classify = lambda question: {"label": "in_scope", "score": .98}
        await service.TeaService(self.runtime).chat("token", "Do you sell tea?")
        self.runtime.intent_reviewer.review.assert_not_awaited()
        self.assertEqual(self.runtime.graph.ainvoke.call_args.args[0]["model_count"], 0)

    async def test_daily_and_session_rate_limits_still_apply(self):
        restricted = service.TeaService(self.runtime, daily_limit=0)
        with self.assertRaisesRegex(RuntimeError, "daily_limit"):
            await restricted.chat("token", "Tea?")
        self.runtime.intent_reviewer.review.assert_not_awaited()
        self.runtime.sessions.data["last_request"] = service.time.time()
        with self.assertRaisesRegex(RuntimeError, "rate_limit"):
            await service.TeaService(self.runtime).chat("token", "Tea?")

    async def test_classifier_failure_does_not_allow_tools(self):
        def fail(question):
            raise RuntimeError("offline classifier")
        self.runtime.classifier.classify = fail
        result = await service.TeaService(self.runtime).chat("token", "Tea?")
        self.assertTrue(result["degraded"])
        self.runtime.intent_reviewer.review.assert_not_awaited()
        self.runtime.graph.ainvoke.assert_not_awaited()

    async def test_review_and_agent_share_four_call_budget(self):
        class LoopModel:
            calls = 0
            async def ask(inner, messages):
                inner.calls += 1
                return AIMessage(content="", tool_calls=[{"name": "search_products", "args": {}, "id": str(inner.calls)}])
        @tool
        async def search_products() -> dict:
            """Return public fixture products."""
            return {"ok": True, "data": {"products": []}}
        model = LoopModel()
        with patch("app.agent.graph.setup_logging"):
            self.runtime.graph = build_graph(model, [search_products])
            result = await service.TeaService(self.runtime).chat("token", "Tea pets?")
        self.assertEqual(model.calls, 3)
        self.assertTrue(result["degraded"])
        self.runtime.intent_reviewer.review.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
