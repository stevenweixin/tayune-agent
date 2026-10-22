"""公网入口的离线契约检查，不启动模型、Milvus或真实会话库。"""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from app.api.rate_limit import RequestRateLimiter, get_visitor_ip
from app.main import create_app


class PublicAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = create_app()
        self.session_count = 0
        self.chat_count = 0

        def create_session():
            self.session_count += 1
            return "fixture-session"

        async def chat(token, question):
            if token != "fixture-session":
                raise LookupError("invalid session")
            self.chat_count += 1
            return {"answer": "Fixture only", "sources": [], "degraded": False, "trace": []}

        self.app.state.runtime = SimpleNamespace(sessions=SimpleNamespace(create=create_session))
        self.app.state.service = SimpleNamespace(chat=chat)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=("127.0.0.1", 12345)),
            base_url="http://local")
        self.headers = {"Origin": "https://tayunetea.com"}
        self.log_patch = patch("app.main.setup_logging", return_value=Mock())
        self.log_patch.start()

    async def asyncTearDown(self):
        self.log_patch.stop()
        await self.client.aclose()

    async def test_preflight_does_not_use_request_budget(self):
        response = await self.client.options("/api/chat", headers={**self.headers,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], self.headers["Origin"])
        self.assertEqual(self.app.state.request_limiter.requests, {})

    async def test_session_rate_limit_and_no_cache(self):
        for _ in range(5):
            response = await self.client.post("/api/session", headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
        response = await self.client.post("/api/session", headers=self.headers)
        self.assertEqual(response.status_code, 429)
        self.assertGreater(int(response.headers["retry-after"]), 0)
        self.assertEqual(self.session_count, 5)

    async def test_missing_or_unknown_session_cannot_call_service(self):
        response = await self.client.post("/api/chat", json={"message": "tea"}, headers=self.headers)
        self.assertEqual(response.status_code, 401)
        response = await self.client.post("/api/chat", json={"message": "tea"},
            headers={**self.headers, "Authorization": "Bearer unknown"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.chat_count, 0)

    async def test_forbidden_origin_cannot_create_session(self):
        response = await self.client.post("/api/session", headers={"Origin": "https://example.org"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.session_count, 0)

    async def test_actual_body_limit_returns_cors_error(self):
        async def chunks():
            yield b"x" * 5000
            yield b"y" * 4000
        response = await self.client.post("/api/chat", content=chunks(), headers=self.headers)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.headers["access-control-allow-origin"], self.headers["Origin"])
        self.assertEqual(self.chat_count, 0)

    async def test_message_length_and_extra_fields_are_rejected(self):
        for body in ({"message": " "}, {"message": "x" * 501}, {"message": "tea", "role": "system"}):
            response = await self.client.post("/api/chat", json=body, headers=self.headers)
            self.assertEqual(response.status_code, 422)
        self.assertEqual(self.chat_count, 0)

    async def test_success_keeps_trace_private(self):
        response = await self.client.post("/api/chat", json={"message": "tea"},
            headers={**self.headers, "Authorization": "Bearer fixture-session"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("trace", response.json())
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.chat_count, 1)


class RateLimitTests(unittest.TestCase):
    def test_cloudflare_header_is_only_trusted_on_loopback(self):
        self.assertEqual(get_visitor_ip("127.0.0.1", "203.0.113.1"), "203.0.113.1")
        self.assertEqual(get_visitor_ip("198.51.100.2", "203.0.113.1"), "198.51.100.2")
        self.assertEqual(get_visitor_ip("127.0.0.1", "invalid"), "127.0.0.1")

    def test_chat_limit_and_expiry(self):
        now = [100.0]
        limiter = RequestRateLimiter(clock=lambda: now[0])
        for _ in range(10):
            self.assertEqual(limiter.check("visitor", "/api/chat"), 0)
        self.assertGreater(limiter.check("visitor", "/api/chat"), 0)
        now[0] += 61
        self.assertEqual(limiter.check("visitor", "/api/chat"), 0)


if __name__ == "__main__":
    unittest.main()
