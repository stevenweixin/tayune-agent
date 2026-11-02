"""有界CPU知识查询与语言过滤回归；不联网、不加载真实模型。"""
import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from langchain_core.documents import Document

from app.agent.answer_guard import SHORT_REPLIES
from app.agent.grounding import check_grounded_answer
from app.agent.knowledge_tool import build_knowledge_tool
from app.core.cpu_worker import CPUWorker
from app.rag.hybrid_retriever import HybridRetriever
from app.rag.reranker import retrieve


def evidence(query="fixture", *args):
    return [(Document(page_content="Approved English evidence.", metadata={
        "title": "Guide", "source_url": "https://tayunetea.com/guide/", "chunk_id": query,
        "language": "en"}), .9)], .9


async def wait_started(event):
    async def wait():
        while not event.is_set():
            await asyncio.sleep(.001)
    await asyncio.wait_for(wait(), 2)


class KnowledgeQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cpu = CPUWorker()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.executed = []

    async def asyncTearDown(self):
        self.release.set()
        await self.cpu.close()

    def blocked_retrieve(self, query, *args):
        self.executed.append(query)
        if len(self.executed) == 1:
            self.entered.set()
            if not self.release.wait(2):
                raise RuntimeError("fixture was not released")
        return evidence(query)

    async def test_parallel_queries_wait_instead_of_failing_cpu_busy(self):
        with patch("app.agent.knowledge_tool.retrieve", side_effect=self.blocked_retrieve):
            tool = build_knowledge_tool(None, None, self.cpu)
            first = asyncio.create_task(tool.ainvoke({"query": "first"}))
            await wait_started(self.entered)
            second = asyncio.create_task(tool.ainvoke({"query": "second"}))
            await asyncio.sleep(.01)
            self.assertEqual(self.executed, ["first"])
            self.assertFalse(second.done())
            self.release.set()
            results = await asyncio.gather(first, second)
        self.assertTrue(all(result["ok"] for result in results))
        self.assertEqual(self.executed, ["first", "second"])

    async def test_queue_is_bounded_to_two_pending_queries(self):
        with patch("app.agent.knowledge_tool.retrieve", side_effect=self.blocked_retrieve):
            tool = build_knowledge_tool(None, None, self.cpu)
            first = asyncio.create_task(tool.ainvoke({"query": "first"}))
            await wait_started(self.entered)
            second = asyncio.create_task(tool.ainvoke({"query": "second"}))
            await asyncio.sleep(.01)
            rejected = await tool.ainvoke({"query": "third"})
            self.assertFalse(rejected["ok"])
            self.assertEqual(self.executed, ["first"])
            self.release.set()
            self.assertTrue(all(result["ok"] for result in await asyncio.gather(first, second)))

    async def test_waiting_request_cancellation_does_not_start_cpu_work(self):
        with patch("app.agent.knowledge_tool.retrieve", side_effect=self.blocked_retrieve):
            tool = build_knowledge_tool(None, None, self.cpu)
            first = asyncio.create_task(tool.ainvoke({"query": "first"}))
            await wait_started(self.entered)
            waiting = asyncio.create_task(tool.ainvoke({"query": "cancelled"}))
            await asyncio.sleep(.01)
            waiting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiting
            self.release.set()
            await first
            recovered = await tool.ainvoke({"query": "recovered"})
            self.assertTrue(recovered["ok"])
            self.assertEqual(self.executed, ["first", "recovered"])

    async def test_timeout_keeps_underlying_cpu_busy_until_thread_finishes(self):
        with patch("app.agent.knowledge_tool.retrieve", side_effect=self.blocked_retrieve), \
                patch("app.agent.knowledge_tool.KNOWLEDGE_TIMEOUT_SECONDS", .04):
            tool = build_knowledge_tool(None, None, self.cpu)
            response = await tool.ainvoke({"query": "timeout"})
            self.assertEqual(response["error"], "tool_timeout")
            self.assertTrue(self.cpu.busy)
            with self.assertRaises(RuntimeError):
                await self.cpu.run(lambda: None)
            self.release.set()
            await self.cpu.close()
            self.assertTrue((await tool.ainvoke({"query": "after-timeout"}))["ok"])

    async def test_language_is_explicitly_passed_to_pipeline(self):
        with patch("app.agent.knowledge_tool.retrieve", side_effect=evidence) as pipeline:
            tool = build_knowledge_tool(None, None, self.cpu)
            result = await tool.ainvoke({"query": "brewing"})
            self.assertTrue(result["ok"])
            self.assertEqual(pipeline.call_args.args[-1], "en")

    async def test_runtime_can_set_larger_but_bounded_knowledge_queue(self):
        with patch("app.agent.knowledge_tool.retrieve", side_effect=self.blocked_retrieve):
            tool = build_knowledge_tool(None, None, self.cpu, max_pending=4)
            first = asyncio.create_task(tool.ainvoke({"query": "first"}))
            await wait_started(self.entered)
            waiting = [asyncio.create_task(tool.ainvoke({"query": f"waiting-{index}"})) for index in range(3)]
            await asyncio.sleep(.01)
            rejected = await tool.ainvoke({"query": "fifth"})
            self.assertEqual(rejected["error"], "knowledge_unavailable")
            self.release.set()
            self.assertTrue(all(item["ok"] for item in await asyncio.gather(first, *waiting)))
            self.assertEqual(len(self.executed), 4)

    async def test_invalid_knowledge_queue_limit_fails_before_serving(self):
        with self.assertRaises(ValueError):
            build_knowledge_tool(None, None, self.cpu, max_pending=0)


class LanguageAndReplyTests(unittest.TestCase):
    def test_dense_and_sparse_have_identical_language_filter(self):
        client = Mock()
        client.hybrid_search.return_value = [[]]
        embedding = SimpleNamespace(encode_query=lambda query: ([.1] * 1024, {1: .2}))
        HybridRetriever(client, embedding).search("brew", source_type="article", language="en")
        requests = client.hybrid_search.call_args.kwargs["reqs"]
        for request in requests:
            self.assertIn('review_status == "approved"', request.expr)
            self.assertIn('metadata["language"] in ["en", "英语（en）"]', request.expr)
            self.assertIn('source_type == "article"', request.expr)

    def test_dense_fallback_keeps_language_filter(self):
        client = Mock()
        client.search.return_value = [[]]
        embedding = SimpleNamespace(encode_query=lambda query: ([.1] * 1024, {}))
        HybridRetriever(client, embedding).search("brew", language="en")
        self.assertIn('metadata["language"] in ["en", "英语（en）"]', client.search.call_args.kwargs["filter"])

    def test_language_is_validated_before_embedding(self):
        embedding = Mock()
        with self.assertRaises(ValueError):
            HybridRetriever(Mock(), embedding).search("brew", language='en" or true')
        embedding.encode_query.assert_not_called()

    def test_pipeline_forwards_language_without_changing_legacy_defaults(self):
        retriever = Mock()
        retriever.search.return_value = []
        reranker = SimpleNamespace(rerank_with_confidence=lambda *args, **kwargs: ([], 0))
        retrieve("brew", retriever, reranker, language="en")
        self.assertEqual(retriever.search.call_args.kwargs["language"], "en")

    def test_scope_and_restricted_are_not_reported_as_outages(self):
        for code in ("<reply:out_of_scope>", "<reply:restricted>"):
            result = check_grounded_answer(code, [])
            self.assertFalse(result["degraded"])
            self.assertEqual(result["answer"], SHORT_REPLIES[code])
            self.assertEqual(result["sources"], [])
        with self.assertRaises(ValueError):
            check_grounded_answer("<reply:out_of_scope> unverified facts", [])


if __name__ == "__main__":
    unittest.main()
