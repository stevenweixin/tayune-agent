"""本地测试页隔离和服务超时回归；不加载模型，不调用付费API。"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from app.agent.service import TeaService, CHAT_TIMEOUT_SECONDS
from app.main import create_app


class LocalWidgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # ASGITransport不运行应用lifespan，所以不会初始化真实Runtime。
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(), client=("127.0.0.1", 12345)),
            base_url="http://127.0.0.1:8011")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_local_page_uses_production_bundle(self):
        result = await self.client.get("/demo/widget")
        self.assertEqual(result.status_code, 200)
        self.assertIn("chatEnabled: true", result.text)
        self.assertIn("/demo/assets/tea_companion.js", result.text)
        self.assertEqual(result.headers["cache-control"], "no-store")
        asset = await self.client.get("/demo/assets/tea_companion.js")
        self.assertEqual(asset.status_code, 200)
        self.assertIn("javascript", asset.headers["content-type"])

    async def test_public_host_cannot_read_local_page_or_asset(self):
        for path in ("/demo/widget", "/demo/assets/tea_companion.js"):
            result = await self.client.get(path, headers={"Host": "agent.tayunetea.com"})
            self.assertEqual(result.status_code, 404)

    async def test_tunnel_header_blocks_even_local_host(self):
        result = await self.client.get("/demo/widget", headers={"CF-Connecting-IP": "203.0.113.1"})
        self.assertEqual(result.status_code, 404)

    async def test_non_loopback_peer_is_rejected(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(), client=("192.0.2.1", 12345)),
            base_url="http://127.0.0.1:8011") as client:
            result = await client.get("/demo/widget")
            self.assertEqual(result.status_code, 404)


class FakeSessions:
    def get(self, token):
        return {"last_request": 0}

    def save(self, token, data):
        pass

    def reserve_request(self, limit):
        return True


class ChatTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_backend_budget_leaves_room_for_widget_stream_delivery(self):
        self.assertEqual(CHAT_TIMEOUT_SECONDS, 85)
        self.assertLess(CHAT_TIMEOUT_SECONDS + 12000 / 12 * 0.02 + 5, 115)

    async def test_timeout_returns_fallback_and_releases_service_lock(self):
        service = TeaService(SimpleNamespace(sessions=FakeSessions()))
        cancelled = []

        async def slow_run(*args):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.append(True)

        service.run = slow_run
        with patch("app.agent.service.CHAT_TIMEOUT_SECONDS", 0.01):
            result = await service.chat("test-token", "test question")
        self.assertTrue(result["degraded"])
        self.assertEqual(result["trace"], ["service:fallback"])
        self.assertEqual(cancelled, [True])
        self.assertFalse(service.lock.locked())


if __name__ == "__main__":
    unittest.main()
