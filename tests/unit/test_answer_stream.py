"""已核验答案的链接结构与SSE传输；全部使用固定数据，不调用模型。"""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from app.agent.answer_guard import check_answer
from app.api.answer_stream import sse_event, stream_answer
from app.main import create_app


SOURCE = {"source_id": "S1", "title": "Example Tea", "url": "https://tayunetea.com/product/example/"}


class InlineLinkTests(unittest.TestCase):
    def test_inline_title_uses_registered_url_and_has_no_duplicate_citation(self):
        result = check_answer("Try [Example Tea](source:S1), USD 20. [S1]", [SOURCE])
        links = [part for part in result["answer_parts"] if part.get("url")]
        self.assertEqual(links, [{"text": "Example Tea", "url": SOURCE["url"]}])
        self.assertNotIn("source:S1", result["answer"])
        self.assertNotIn("[S1]", result["answer"])

    def test_legacy_citation_is_linked_where_it_occurs(self):
        result = check_answer("Recipe. [S1] Follow this. [S1]", [SOURCE])
        self.assertEqual(result["answer"].count("Example Tea"), 1)
        self.assertIn("Recipe.  (Example Tea) Follow this.", result["answer"])

    def test_unknown_link_id_is_rejected_even_with_a_valid_citation(self):
        with self.assertRaisesRegex(ValueError, "unknown_source_id"):
            check_answer("[Invented Tea](source:S9). [S1]", [SOURCE])

    def test_malformed_internal_link_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid_inline_link"):
            check_answer("[Tea](source:unknown). [S1]", [SOURCE])

    def test_model_url_does_not_become_a_clickable_part(self):
        result = check_answer("[Tea](https://example.org/fake). [S1]", [SOURCE])
        links = [part["url"] for part in result["answer_parts"] if part.get("url")]
        self.assertEqual(links, [SOURCE["url"]])

    def test_internal_reference_has_no_fake_link_or_identifier(self):
        source = {"source_id": "S1", "title": "Approved guide", "url": None, "evidence_key": "private-id"}
        result = check_answer("Use the [shop guide](source:S1).", [source])
        self.assertFalse(any(part.get("url") for part in result["answer_parts"]))
        self.assertIn("no public link", result["answer"])
        self.assertNotIn("private-id", result["answer"])

    def test_selected_sources_and_display_parts_stay_consistent(self):
        other = {"source_id": "S2", "title": "Other", "url": "https://tayunetea.com/other/"}
        result = check_answer("Try [Tea](source:S1).", [SOURCE, other])
        self.assertEqual(result["sources"], [{"title": SOURCE["title"], "url": SOURCE["url"]}])
        self.assertEqual("".join(part["text"] for part in result["answer_parts"]).strip(), result["answer"])


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_reconstructs_unicode_and_link_parts(self):
        result = {"answer": "茶🌿" * 15, "answer_parts": [{"text": "茶🌿" * 15, "url": SOURCE["url"]}],
                  "request_id": "fixture-id", "degraded": False, "trace": ["private"]}
        with patch("app.api.answer_stream.asyncio.sleep", new=AsyncMock()):
            frames = [frame async for frame in stream_answer(result)]
        events = []
        for frame in frames:
            lines = frame.strip().split("\n")
            events.append((lines[0][7:], json.loads(lines[1][6:])))
        self.assertEqual(events[0][0], "meta")
        self.assertEqual(events[-1][0], "done")
        deltas = [data for event, data in events if event == "delta"]
        self.assertEqual(len(deltas), 3)
        self.assertEqual("".join(data["text"] for data in deltas), result["answer"])
        self.assertTrue(all(data["url"] == SOURCE["url"] for data in deltas))
        self.assertNotIn("private", "".join(frames))

    async def test_event_payload_cannot_inject_new_sse_events(self):
        value = "Tea\n\nevent: done\ndata: evil"
        frame = sse_event("delta", {"text": value})
        self.assertEqual(frame.count("\n\n"), 1)
        self.assertEqual(json.loads(frame.split("data: ", 1)[1])["text"], value)

    async def test_cancellation_does_not_continue_delivery(self):
        result = {"answer": "x" * 50, "request_id": "fixture", "degraded": False}
        generator = stream_answer(result)
        self.assertIn("event: meta", await anext(generator))
        self.assertIn("event: delta", await anext(generator))
        await generator.aclose()
        with self.assertRaises(StopAsyncIteration):
            await anext(generator)


class StreamAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = create_app()
        self.service = SimpleNamespace(chat=AsyncMock(return_value={
            **check_answer("Try [Example Tea](source:S1).", [SOURCE]), "trace": ["private"]}))
        self.app.state.service = self.service
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://local")
        self.headers = {"Authorization": "Bearer fixture", "Origin": "https://tayunetea.com",
                        "Accept": "text/event-stream"}
        self.log_patch = patch("app.main.setup_logging", return_value=Mock())
        self.log_patch.start()

    async def asyncTearDown(self):
        self.log_patch.stop()
        await self.client.aclose()

    async def test_sse_uses_one_service_call_and_preserves_cors_and_cache_controls(self):
        response = await self.client.post("/api/chat", headers=self.headers, json={"message": "tea"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/event-stream", response.headers["content-type"])
        self.assertEqual(response.headers["cache-control"], "no-store, no-transform")
        self.assertEqual(response.headers["x-accel-buffering"], "no")
        self.assertEqual(response.headers["access-control-allow-origin"], self.headers["Origin"])
        self.assertIn("event: done", response.text)
        self.assertNotIn("private", response.text)
        self.service.chat.assert_awaited_once()

    async def test_old_json_contract_is_preserved(self):
        response = await self.client.post("/api/chat", headers={"Authorization": "Bearer fixture"}, json={"message": "tea"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["answer_parts"][1]["url"], SOURCE["url"])
        self.assertNotIn("trace", response.json())

    async def test_invalid_session_is_http_error_before_stream_headers(self):
        self.service.chat.side_effect = LookupError("expired")
        response = await self.client.post("/api/chat", headers=self.headers, json={"message": "tea"})
        self.assertEqual(response.status_code, 401)
        self.assertIn("application/json", response.headers["content-type"])

    async def test_fallback_is_delivered_without_links(self):
        self.service.chat.return_value = {"answer": "Unavailable", "sources": [], "degraded": True}
        response = await self.client.post("/api/chat", headers=self.headers, json={"message": "tea"})
        self.assertIn('"degraded": true', response.text)
        self.assertNotIn('"url"', response.text)


if __name__ == "__main__":
    unittest.main()
