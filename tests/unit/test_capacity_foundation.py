"""容量底座：会话扩容/过期、SQL热路径、有界等待及分类分道，全部离线。"""
import asyncio
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app.agent.service import TeaService
from app.core.concurrency import RequestCoordinator, read_capacity_settings
from app.core.execution import CURRENT_RUN, Execution, encode_state
from app.core.runtime import ROOT, Runtime
from app.database.runs import RunStore
from app.database.session import SessionStore
from app.main import create_app


class CapacitySettingsTests(unittest.TestCase):
    def test_runtime_defaults_and_invalid_limits(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = read_capacity_settings()
        self.assertEqual(settings, {"max_concurrent_chats": 4, "max_waiting_chats": 16,
                                   "admission_wait_seconds": 2, "max_sessions": 5000})
        for key, value in (("TAYUNE_MAX_SESSIONS", "0"), ("TAYUNE_MAX_SESSIONS", "100001"),
                           ("TAYUNE_MAX_WAITING_CHATS", "129"), ("TAYUNE_ADMISSION_WAIT_SECONDS", "nan"),
                           ("TAYUNE_ADMISSION_WAIT_SECONDS", "6")):
            with patch.dict(os.environ, {key: value}, clear=True), self.assertRaises(ValueError):
                read_capacity_settings()


class SessionCapacityTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "reports").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "reports")
        self.path = Path(self.temp.name) / "session.sqlite3"
        self.store = SessionStore(self.path, max_sessions=5000)
        self.runs = RunStore(self.store)
        self.store.runs = self.runs

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_three_thousand_sessions_use_formal_create_not_seeding(self):
        tokens = [self.store.create() for _ in range(3000)]
        self.assertEqual(len(set(tokens)), 3000)
        self.assertIsNotNone(self.store.get(tokens[-1]))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 3000)

    def test_valid_reads_do_not_run_global_cleanup_or_commit(self):
        token = self.store.create()
        statements = []
        self.store.db.set_trace_callback(lambda sql: statements.append(sql.split()[0]))
        for _ in range(100):
            self.assertIsNotNone(self.store.get(token))
        self.store.db.set_trace_callback(None)
        self.assertEqual(statements, ["SELECT"] * 100)

    def test_periodic_cleanup_is_throttled(self):
        with patch("app.database.session.time.monotonic", return_value=100):
            self.assertTrue(self.store.cleanup())
            self.assertFalse(self.store.cleanup())
        with patch("app.database.session.time.monotonic", return_value=161):
            self.assertTrue(self.store.cleanup())

    def test_expired_access_immediately_erases_private_execution(self):
        token = self.store.create()
        run_id = self.runs.begin(token, "expiry", "fixture", {}, 20)
        self.runs.event(run_id, "fixture", {})
        self.store.db.execute("UPDATE sessions SET updated=0 WHERE token=?", (token,))
        self.store.db.commit()
        self.assertIsNone(self.store.get(token))
        self.assertEqual(self.runs.audit(run_id), [])

    def test_expired_rows_do_not_consume_live_capacity_between_cleanups(self):
        self.store.max_sessions = 1
        old = self.store.create()
        with self.assertRaises(RuntimeError):
            self.store.create()
        self.store.db.execute("UPDATE sessions SET updated=0 WHERE token=?", (old,))
        self.store.db.commit()
        new = self.store.create()
        self.assertIsNotNone(self.store.get(new))
        self.assertIsNone(self.store.get(old))

    def test_wal_and_cleanup_audit_indexes_are_present(self):
        self.assertEqual(self.store.db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.store.db.execute("PRAGMA synchronous").fetchone()[0], 2)
        for sql, index in (("SELECT token FROM sessions WHERE updated<1", "sessions_updated"),
                           ("SELECT id FROM task_runs WHERE owner='fixture'", "task_runs_owner"),
                           ("SELECT * FROM execution_events WHERE run_id='fixture' ORDER BY rowid", "execution_events_run")):
            plan = str(self.store.db.execute("EXPLAIN QUERY PLAN " + sql).fetchall())
            self.assertIn(index, plan)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_existing_database_keeps_session_and_budget(self):
        token = self.store.create()
        self.assertTrue(self.store.reserve_request(20))
        self.store.close()
        self.store = SessionStore(self.path, max_sessions=5000)
        self.assertIsNotNone(self.store.get(token))
        self.assertEqual(self.store.db.execute("SELECT count FROM budget").fetchone()[0], 1)


class AdmissionQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_fifo_and_queue_full(self):
        gate = RequestCoordinator(1, max_waiting=2, wait_seconds=1)
        order = []
        async def waiting(number):
            async with gate.admit():
                order.append(number)
                await asyncio.sleep(0)
        with gate.slot():
            first = asyncio.create_task(waiting(1))
            second = asyncio.create_task(waiting(2))
            await asyncio.sleep(.01)
            with self.assertRaisesRegex(RuntimeError, "queue_full"):
                async with gate.admit():
                    pass
        await asyncio.gather(first, second)
        self.assertEqual(order, [1, 2])
        self.assertEqual(gate.snapshot()["active"], 0)
        self.assertEqual(gate.snapshot()["waiting"], 0)

    async def test_wait_timeout_never_starts_work(self):
        gate = RequestCoordinator(1, max_waiting=1, wait_seconds=.01)
        with gate.slot():
            with self.assertRaisesRegex(RuntimeError, "admission_timeout"):
                async with gate.admit():
                    self.fail("Timed out waiter must not execute")
        self.assertEqual(gate.timed_out, 1)
        self.assertEqual(gate.active, 0)
        self.assertFalse(gate.waiters)

    async def test_cancel_after_handoff_returns_reserved_slot(self):
        for _ in range(25):
            gate = RequestCoordinator(1, max_waiting=1)
            async def waiting():
                async with gate.admit():
                    await asyncio.sleep(1)
            with gate.slot():
                task = asyncio.create_task(waiting())
                await asyncio.sleep(0)
                self.assertEqual(len(gate.waiters), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(gate.active, 0)
            self.assertFalse(gate.waiters)

    async def test_cancel_queued_waiter_does_not_consume_a_future_slot(self):
        gate = RequestCoordinator(1, max_waiting=1)
        async def waiting():
            async with gate.admit():
                self.fail("Cancelled waiter must not run")
        with gate.slot():
            task = asyncio.create_task(waiting())
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        async with gate.admit():
            self.assertEqual(gate.active, 1)
        self.assertEqual(gate.active, 0)


class QueuedServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        (ROOT / "reports").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "reports")
        self.store = SessionStore(Path(self.temp.name) / "session.sqlite3")
        self.runs = RunStore(self.store)
        self.store.runs = self.runs
        self.runtime = SimpleNamespace(sessions=self.store, runs=self.runs, versions={"code": "fixture"})
        self.service = TeaService(self.runtime, max_concurrent=1, max_waiting=1, wait_seconds=.5)
        self.tokens = [self.store.create() for _ in range(3)]
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.tasks = []
        async def running(data, token, question):
            CURRENT_RUN.get().reserve_model()
            self.entered.set()
            await self.release.wait()
            return {"answer": "fixture", "degraded": False, "sources": []}
        self.service.run = running

    async def asyncTearDown(self):
        self.release.set()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.store.close()
        self.temp.cleanup()

    def start(self, index):
        task = asyncio.create_task(self.service.chat(self.tokens[index], "fixture", f"request-{index}"))
        self.tasks.append(task)
        return task

    async def wait_queued(self):
        for _ in range(100):
            if self.service.requests.waiters:
                return
            await asyncio.sleep(.001)
        self.fail("Expected a queued request")

    async def test_queued_has_owner_but_no_task_or_quota_until_execution(self):
        first = self.start(0)
        await self.entered.wait()
        second = self.start(1)
        await self.wait_queued()
        self.assertIn(self.tokens[1], self.service.requests.owners)
        self.assertEqual(self.store.db.execute("SELECT count FROM budget").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0], 1)
        with self.assertRaisesRegex(RuntimeError, "queue_full"):
            await self.service.chat(self.tokens[2], "fixture", "overflow")
        self.release.set()
        results = await asyncio.gather(first, second)
        self.assertEqual(len({row["task_id"] for row in results}), 2)
        self.assertEqual(self.store.db.execute("SELECT count FROM budget").fetchone()[0], 2)
        self.assertEqual(self.service.requests.snapshot()["owners"], 0)

    async def test_timeout_does_not_register_charge_or_leave_owner(self):
        self.service.requests.wait_seconds = .01
        self.start(0)
        await self.entered.wait()
        with self.assertRaisesRegex(RuntimeError, "admission_timeout"):
            await self.service.chat(self.tokens[1], "fixture", "waiting")
        self.assertEqual(self.store.db.execute("SELECT count FROM budget").fetchone()[0], 1)
        self.assertEqual(self.store.get(self.tokens[1])["last_request"], 0)
        self.assertNotIn(self.tokens[1], self.service.requests.owners)

    async def test_cancel_queued_request_never_calls_model_or_charges(self):
        self.start(0)
        await self.entered.wait()
        waiting = self.start(1)
        await self.wait_queued()
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0], 1)
        self.assertEqual(len(self.service.requests.waiters), 0)
        self.assertNotIn(self.tokens[1], self.service.requests.owners)

    async def test_http_queue_full_returns_backoff_without_charging(self):
        self.start(0)
        await self.entered.wait()
        self.start(1)
        await self.wait_queued()
        app = create_app()
        app.state.runtime, app.state.service = self.runtime, self.service
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8012") as client:
            response = await client.post("/api/chat", headers={"Authorization": "Bearer " + self.tokens[2]},
                json={"message": "fixture", "request_id": "overflow-request-0001"})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "2")
        self.assertEqual(self.store.db.execute("SELECT count FROM budget").fetchone()[0], 1)

    async def test_session_expiry_during_wait_is_rechecked(self):
        self.start(0)
        await self.entered.wait()
        waiting = self.start(1)
        await self.wait_queued()
        self.store.db.execute("UPDATE sessions SET updated=0 WHERE token=?", (self.tokens[1],))
        self.store.db.commit()
        self.release.set()
        with self.assertRaises(LookupError):
            await waiting
        self.assertEqual(self.store.db.execute("SELECT count FROM budget").fetchone()[0], 1)

    async def test_resume_rechecks_evidence_age_after_wait(self):
        now = time.time()
        run_id = self.runs.begin(self.tokens[1], "resume", "fixture", self.runtime.versions, 20)
        state = {"messages": [], "dialogue": [], "summary": "", "evidence_started_at": now - 59.9,
                 "session_revision": 0, "memory_revision": 0}
        self.runs.checkpoint(run_id, encode_state(state), "model", Execution(run_id, self.runs, {}).counters())
        self.runs.finish(run_id, "interrupted", None, {})
        with patch("app.agent.service.time.time", return_value=now):
            self.start(0)
            await self.entered.wait()
            waiting = asyncio.create_task(self.service.resume(self.tokens[1], run_id))
            self.tasks.append(waiting)
            await self.wait_queued()
        with patch("app.agent.service.time.time", return_value=now + .2):
            self.release.set()
            with self.assertRaises(ValueError):
                await waiting
        self.assertEqual(self.runs.get(self.tokens[1], run_id)["status"], "interrupted")


class ClassifierLaneTests(unittest.IsolatedAsyncioTestCase):
    async def test_classifier_can_run_while_rag_lane_is_occupied(self):
        runtime = Runtime()
        entered, release = threading.Event(), threading.Event()
        def blocked_rag():
            entered.set()
            release.wait(3)
        task = asyncio.create_task(runtime.cpu.run(blocked_rag))
        try:
            while not entered.is_set():
                await asyncio.sleep(.001)
            result = await asyncio.wait_for(runtime.classifier_cpu.run(lambda: "classified"), .5)
            self.assertEqual(result, "classified")
            self.assertFalse(task.done())
        finally:
            release.set()
            await task
            await runtime.close()

    async def test_service_routes_classification_to_its_lane(self):
        fast = SimpleNamespace(run=AsyncMock(return_value={"label": "restricted", "score": .9}))
        slow = SimpleNamespace(run=AsyncMock(side_effect=AssertionError("RAG lane must not classify")))
        runtime = SimpleNamespace(cpu=slow, classifier_cpu=fast, classifier=SimpleNamespace(classify=lambda q: None),
            intent_reviewer=SimpleNamespace(review=AsyncMock(return_value={"decision": "RESTRICTED",
                                            "usage": {"input_tokens": 0, "output_tokens": 0}})))
        result = await TeaService(runtime).run({"history": [], "summary": ""}, "fixture", "private data")
        self.assertFalse(result["degraded"])
        self.assertEqual(fast.run.await_count, 1)
        self.assertEqual(slow.run.await_count, 0)
