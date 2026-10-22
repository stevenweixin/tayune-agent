"""Offline final-answer citation and bounded graph-loop regression tests."""
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agent.answer_guard import FALLBACK, SHORT_REPLIES, check_answer
from app.agent.graph import build_graph


SOURCE = {"source_id": "S1", "title": "Brewing guide", "url": "https://example.com/brewing"}


class AnswerGuardTests(unittest.TestCase):
    def test_uncited_recipe_is_rejected(self):
        for sources in ([], [SOURCE]):
            with self.assertRaisesRegex(ValueError, "missing_citations"):
                check_answer("Use 5 g in 300 ml for 2 minutes.", sources)

    def test_only_cited_current_sources_are_selected(self):
        other = {"source_id": "S2", "title": "Unrelated page", "url": "https://example.com/other"}
        result = check_answer("Use the mug method. [S1] Remove the infuser. [S1]", [SOURCE, other])
        self.assertEqual(result["sources"], [{"title": SOURCE["title"], "url": SOURCE["url"]}])
        self.assertNotIn("[S1]", result["answer"])

    def test_unknown_or_old_turn_id_is_rejected(self):
        for text, sources in (("Recipe. [S9]", [SOURCE]), ("Recipe. [S1]", [])):
            with self.assertRaisesRegex(ValueError, "unknown_source_id"):
                check_answer(text, sources)

    def test_empty_or_marker_only_answer_is_rejected(self):
        for text in ("", "  ", "[S1]"):
            with self.assertRaisesRegex(ValueError, "empty_answer"):
                check_answer(text, [SOURCE])

    def test_short_replies_are_program_text_not_model_prose(self):
        for code, expected in SHORT_REPLIES.items():
            result = check_answer(code, [SOURCE])
            self.assertEqual(result["answer"], expected)
            self.assertEqual(result["sources"], [])
            self.assertEqual(result["degraded"], code == "<reply:unavailable>")

    def test_code_cannot_smuggle_an_uncited_recipe(self):
        for text in ("<reply:brewing_setup> Use 5 g.", "<reply:unknown>"):
            with self.assertRaisesRegex(ValueError, "invalid_reply_code"):
                check_answer(text, [])


class FakeModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def ask(self, messages):
        self.calls.append(list(messages))
        return self.responses.pop(0)


class FakeKnowledge:
    name = "search_knowledge"

    async def ainvoke(self, args):
        return {"ok": True, "passages": [{
            "title": "Brewing guide", "url": "https://example.com/brewing",
            "content": "Use the mug method and remove the infuser.",
        }]}


def tool_request(call_id="lookup-1", name="search_knowledge"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": call_id}])


class GraphGuardTests(unittest.IsolatedAsyncioTestCase):
    def make_graph(self, model):
        with patch("app.agent.graph.setup_logging"):
            return build_graph(model, [FakeKnowledge()])

    def initial_state(self, **changes):
        state = {
            "messages": [HumanMessage(content="How should I brew that tea in a mug?")],
            "answer": "", "sources": [], "known_product_ids": [], "trace": [],
            "model_count": 0, "tool_count": 0, "degraded": False,
        }
        state.update(changes)
        return state

    async def run_graph(self, model, **changes):
        graph = self.make_graph(model)
        return await graph.ainvoke(self.initial_state(**changes), config={"recursion_limit": 12})

    async def test_unsafe_draft_repairs_via_tool_then_cites(self):
        model = FakeModel([
            AIMessage(content="UNVERIFIED: use 5 g in 300 ml."),
            tool_request(),
            AIMessage(content="Use the mug method and remove the infuser. [S1]"),
        ])
        state = await self.run_graph(model)
        self.assertFalse(state["degraded"])
        self.assertEqual(state["model_count"], 3)
        self.assertEqual(state["tool_count"], 1)
        self.assertEqual(state["answer_repair_count"], 1)
        self.assertEqual(len(state["sources"]), 1)
        self.assertIn("answer_guard:repair", state["trace"])
        self.assertIsInstance(model.calls[1][-1], SystemMessage)
        for message in model.calls[1]:
            self.assertNotIn("UNVERIFIED", message.content)

    async def test_second_rejection_falls_back_without_third_call(self):
        model = FakeModel([AIMessage(content="Use 5 g."), AIMessage(content="Use 6 g.")])
        state = await self.run_graph(model)
        self.assertEqual(state["answer"], FALLBACK)
        self.assertEqual(len(model.calls), 2)
        self.assertTrue(state["degraded"])
        self.assertEqual(state["sources"], [])

    async def test_fourth_call_rejection_cannot_reset_budget(self):
        model = FakeModel([AIMessage(content="Use 5 g.")])
        state = await self.run_graph(model, model_count=3)
        self.assertEqual(state["model_count"], 4)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(state["answer"], FALLBACK)

    async def test_successful_cited_path_uses_no_repair(self):
        model = FakeModel([tool_request(), AIMessage(content="Use the mug method. [S1]")])
        state = await self.run_graph(model)
        self.assertEqual(state.get("answer_repair_count", 0), 0)
        self.assertEqual(len(model.calls), 2)

    async def test_clarification_does_not_require_a_tool(self):
        model = FakeModel([AIMessage(content="<reply:tea_preferences>")])
        state = await self.run_graph(model)
        self.assertEqual(state["tool_count"], 0)
        self.assertFalse(state["degraded"])
        self.assertNotIn("<reply:", state["answer"])

    async def test_repair_cannot_expand_total_model_budget(self):
        model = FakeModel([
            AIMessage(content="Use 5 g."),
            tool_request("one"), tool_request("two"), tool_request("three"),
        ])
        state = await self.run_graph(model)
        self.assertEqual(state["model_count"], 4)
        self.assertEqual(state["tool_count"], 3)
        self.assertTrue(state["degraded"])
        self.assertEqual(len(model.calls), 4)

    async def test_fake_tool_requests_still_consume_tool_budget(self):
        calls = []
        for index in range(5):
            calls.append({"name": "fake_tool", "args": {}, "id": str(index)})
        model = FakeModel([AIMessage(content="", tool_calls=calls), AIMessage(content="<reply:unavailable>")])
        state = await self.run_graph(model)
        self.assertEqual(state["tool_count"], 4)
        self.assertTrue(state["degraded"])

    async def test_repair_state_is_not_shared_between_requests(self):
        model = FakeModel([
            AIMessage(content="First unsafe draft."), AIMessage(content="<reply:unavailable>"),
            AIMessage(content="Second unsafe draft."), AIMessage(content="<reply:unavailable>"),
        ])
        graph = self.make_graph(model)
        for _ in range(2):
            state = await graph.ainvoke(self.initial_state(), config={"recursion_limit": 12})
            self.assertEqual(state["answer_repair_count"], 1)
        self.assertEqual(len(model.calls), 4)


if __name__ == "__main__":
    unittest.main()
