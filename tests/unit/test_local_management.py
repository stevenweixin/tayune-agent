"""管理接口权限/记忆隔离离线检查；不用真实Runtime。"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from app.agent.service import TeaService
from app.database.runs import RunStore
from app.database.session import SessionStore
from app.main import create_app


class ManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        sessions = SessionStore(Path(self.temp.name) / "fixture.sqlite3")
        self.runtime = SimpleNamespace(sessions=sessions, runs=RunStore(sessions), versions={"code": "fixture"})
        sessions.runs = self.runtime.runs
        self.token = sessions.create()
        self.app = create_app()
        self.app.state.runtime = self.runtime
        self.app.state.service = TeaService(self.runtime)
        self.app.state.service.run = AsyncMock(return_value={"answer": "fixture", "sources": [], "degraded": False, "trace": []})
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, client=("127.0.0.1", 12345)), base_url="http://127.0.0.1:8011")
        self.headers = {"Authorization": "Bearer " + self.token}
        self.log = patch("app.main.setup_logging", return_value=Mock())
        self.log.start()

    async def asyncTearDown(self):
        self.log.stop()
        await self.client.aclose()
        self.runtime.sessions.close()
        self.temp.cleanup()

    async def test_cloudflare_forwarded_management_is_not_public(self):
        response = await self.client.get("/api/local/memory", headers={**self.headers, "CF-Connecting-IP": "203.0.113.1"})
        self.assertEqual(response.status_code, 404)
        response = await self.client.get("https://agent.tayunetea.com/api/local/memory", headers=self.headers)
        self.assertEqual(response.status_code, 404)

    async def test_explicit_preferences_can_be_viewed_corrected_and_deleted(self):
        response = await self.client.post("/api/local/memory", headers=self.headers, json={"budget_usd": 50, "vessel": "mug"})
        self.assertEqual(response.status_code, 200)
        response = await self.client.post("/api/local/memory", headers=self.headers, json={"budget_usd": 30})
        self.assertEqual(response.json()["preferences"]["budget_usd"]["value"], 30)
        response = await self.client.get("/api/local/memory", headers=self.headers)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json()["preferences"]["vessel"]["value"], "mug")
        response = await self.client.delete("/api/local/memory", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual((await self.client.get("/api/local/memory", headers=self.headers)).status_code, 401)

    async def test_same_id_same_payload_replays_and_different_payload_conflicts(self):
        body = {"message": "tea", "request_id": "fixture-request-0001"}
        first = await self.client.post("/api/chat", headers=self.headers, json=body)
        second = await self.client.post("/api/chat", headers=self.headers, json=body)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), second.json())
        response = await self.client.post("/api/chat", headers=self.headers, json={**body, "message": "changed"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.app.state.service.run.await_count, 1)

    async def test_foreign_session_cannot_inspect_task(self):
        run_id = self.runtime.runs.begin(self.token, "fixture-request", "tea", {}, 20)
        other = self.runtime.sessions.create()
        response = await self.client.get("/api/local/tasks/" + run_id, headers={"Authorization": "Bearer " + other})
        self.assertEqual(response.status_code, 404)

    async def test_unknown_preference_fields_are_not_saved(self):
        response = await self.client.post("/api/local/memory", headers=self.headers, json={"medical_history": "private"})
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
