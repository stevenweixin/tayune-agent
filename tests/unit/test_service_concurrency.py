"""正式并发控制：会话、额度、恢复、取消、CPU排队；无网络和付费模型。"""
import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from langchain_core.messages import HumanMessage

from app.agent.service import TeaService
from app.core.concurrency import RequestCoordinator, read_chat_capacity
from app.core.cpu_worker import CPUQueue
from app.core.execution import CURRENT_RUN, Execution, encode_state
from app.database.runs import RunStore
from app.database.session import SessionStore
from app.main import create_app


class CoordinatorTests(unittest.TestCase):
    def test_default_and_configured_capacity(self):
        with patch.dict("os.environ", {"TAYUNE_MAX_CONCURRENT_CHATS": "4"}):
            self.assertEqual(read_chat_capacity(), 4)
        for value in ("0", "33", "invalid"):
            with patch.dict("os.environ", {"TAYUNE_MAX_CONCURRENT_CHATS": value}):
                with self.assertRaises(ValueError):
                    read_chat_capacity()

    def test_same_session_and_capacity_are_independent(self):
        gate = RequestCoordinator(1)
        with gate.session("a"), gate.slot():
            with self.assertRaisesRegex(RuntimeError, "session_busy"):
                with gate.session("a"):
                    pass
            # 另一个会话可以管理记忆，但不能绕过聊天容量。
            with gate.session("b"):
                with self.assertRaisesRegex(RuntimeError, "busy"):
                    with gate.slot():
                        pass
        self.assertEqual(gate.active, 0)
        self.assertEqual(gate.owners, set())


class ConcurrentServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SessionStore(Path(self.temp.name) / "fixture.sqlite3")
        self.runs = RunStore(self.store)
        self.store.runs = self.runs
        self.runtime = SimpleNamespace(sessions=self.store, runs=self.runs, versions={"code": "fixture"})
        self.service = TeaService(self.runtime, max_concurrent=4)
        self.tokens = [self.store.create() for _ in range(6)]
        self.entered = asyncio.Queue()
        self.release = asyncio.Event()
        self.tasks = []

        async def running(data, token, question):
            execution = CURRENT_RUN.get()
            execution.reserve_model()
            await self.entered.put(token)
            await self.release.wait()
            execution.record_usage({"input_tokens": len(question), "output_tokens": 1})
            data["history"] = [{"role": "human", "text": question}, {"role": "ai", "text": token[-4:]}]
            self.store.save(token, data)
            return {"answer": "fixture", "sources": [], "degraded": False}

        self.service.run = running
        self.app = create_app()
        self.app.state.runtime = self.runtime
        self.app.state.service = self.service
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, client=("127.0.0.1", 1)),
                                        base_url="http://127.0.0.1:8011")

    async def asyncTearDown(self):
        self.release.set()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.client.aclose()
        self.store.close()
        self.temp.cleanup()

    def start_chat(self, index, question="fixture"):
        task = asyncio.create_task(self.service.chat(self.tokens[index], question, f"request-{index:04d}"))
        self.tasks.append(task)
        return task

    async def wait_entered(self, count=1):
        for _ in range(count):
            await asyncio.wait_for(self.entered.get(), 2)

    async def test_four_sessions_overlap_fifth_rejected_without_quota(self):
        tasks = [self.start_chat(index, "q" * (index + 1)) for index in range(4)]
        await self.wait_entered(4)
        self.assertEqual(self.service.requests.active, 4)
        with self.assertRaisesRegex(RuntimeError, "busy"):
            await self.service.chat(self.tokens[4], "extra", "request-extra")
        self.release.set()
        results = await asyncio.gather(*tasks)
        self.assertEqual(len({item["task_id"] for item in results}), 4)
        for index, result in enumerate(results):
            self.assertEqual(result["model_calls"], 1)
            self.assertEqual(result["usage"]["input_tokens"], index + 1)
            self.assertEqual(self.store.get(self.tokens[index])["history"][0]["text"], "q" * (index + 1))
        self.assertEqual(self.store.db.execute("SELECT count FROM budget").fetchone()[0], 4)
        self.assertEqual(self.service.requests.owners, set())
        self.assertIsNone(CURRENT_RUN.get())

    async def test_same_session_duplicate_does_not_start_or_charge_again(self):
        first = self.start_chat(0)
        await self.wait_entered()
        with self.assertRaisesRegex(RuntimeError, "session_busy"):
            await self.service.chat(self.tokens[0], "fixture", "request-0000")
        self.release.set()
        result = await first
        replay = await self.service.chat(self.tokens[0], "fixture", "request-0000")
        self.assertEqual(result["task_id"], replay["task_id"])
        self.assertEqual(self.store.db.execute("SELECT count FROM budget").fetchone()[0], 1)

    async def test_cached_replay_works_when_other_session_uses_last_slot(self):
        self.release.set()
        completed = await self.start_chat(0)
        await self.wait_entered()
        self.release.clear()
        self.service.requests = RequestCoordinator(1)
        other = self.start_chat(1)
        await self.wait_entered()
        replay = await self.service.chat(self.tokens[0], "fixture", "request-0000")
        self.assertEqual(completed["task_id"], replay["task_id"])
        self.assertFalse(other.done())

    async def test_concurrent_daily_quota_is_atomic(self):
        self.service.daily_limit = 1
        first = self.start_chat(0)
        await self.wait_entered()
        for index in range(1, 4):
            with self.assertRaisesRegex(RuntimeError, "daily_limit"):
                await self.service.chat(self.tokens[index], "fixture", f"quota-{index}")
        self.assertEqual(self.store.db.execute("SELECT count FROM budget").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0], 1)
        self.assertEqual(self.service.requests.active, 1)
        self.release.set()
        await first

    async def test_memory_writes_block_only_the_active_owner(self):
        self.start_chat(0)
        await self.wait_entered()
        own = {"Authorization": "Bearer " + self.tokens[0]}
        other = {"Authorization": "Bearer " + self.tokens[1]}
        self.assertEqual((await self.client.post("/api/local/memory", headers=own, json={"budget_usd": 30})).status_code, 409)
        self.assertEqual((await self.client.delete("/api/local/memory", headers=own)).status_code, 409)
        self.assertEqual((await self.client.post("/api/local/memory", headers=other, json={"budget_usd": 30})).status_code, 200)
        self.assertEqual((await self.client.delete("/api/local/memory", headers=other)).status_code, 200)

    async def test_cancel_does_not_cancel_another_session(self):
        first, other = self.start_chat(0), self.start_chat(1)
        await self.wait_entered(2)
        saved = self.runs.by_request_key(self.tokens[0], "request-0000")
        with self.assertRaises(LookupError):
            self.service.cancel(self.tokens[1], saved["id"])
        self.service.cancel(self.tokens[0], saved["id"])
        self.assertEqual((await first)["task_status"], "cancelled")
        self.assertFalse(other.done())
        self.assertEqual(self.service.requests.active, 1)

    async def test_http_caller_cancellation_releases_owner_and_capacity(self):
        task = self.start_chat(0)
        await self.wait_entered()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.service.requests.active, 0)
        self.assertEqual(self.service.requests.owners, set())
        saved = self.runs.by_request_key(self.tokens[0], "request-0000")
        self.assertEqual(saved["status"], "cancelled")

    def make_resumable(self, index):
        token = self.tokens[index]
        run_id = self.runs.begin(token, f"resume-{index}", "fixture", self.runtime.versions, 20)
        state = {"messages": [HumanMessage(content="fixture")], "dialogue": [{"role": "human", "text": "fixture"}],
                 "summary": "", "evidence_started_at": time.time(), "session_revision": 0,
                 "memory_revision": 0, "answer": "fixture", "sources": [], "degraded": False}
        counters = Execution(run_id, self.runs, self.runtime.versions).counters()
        self.runs.checkpoint(run_id, encode_state(state), "model", counters)
        self.runs.finish(run_id, "interrupted", None, counters)
        return run_id

    async def test_resume_uses_same_session_owner_and_blocks_memory_writes(self):
        run_id = self.make_resumable(0)

        async def graph(state, **kwargs):
            await self.entered.put(self.tokens[0])
            await self.release.wait()
            return state

        self.runtime.graph = SimpleNamespace(ainvoke=graph)
        resume = asyncio.create_task(self.service.resume(self.tokens[0], run_id))
        self.tasks.append(resume)
        await self.wait_entered()
        with self.assertRaisesRegex(RuntimeError, "session_busy"):
            await self.service.chat(self.tokens[0], "new question", "new-request")
        with self.assertRaisesRegex(RuntimeError, "session_busy"):
            await self.service.resume(self.tokens[0], run_id)
        response = await self.client.delete("/api/local/memory", headers={"Authorization": "Bearer " + self.tokens[0]})
        self.assertEqual(response.status_code, 409)
        self.release.set()
        self.assertEqual((await resume)["task_status"], "completed")

    async def test_resume_cannot_bypass_capacity_or_claim_checkpoint_on_rejection(self):
        self.service.requests = RequestCoordinator(1)
        run_id = self.make_resumable(1)
        self.start_chat(0)
        await self.wait_entered()
        with self.assertRaisesRegex(RuntimeError, "busy"):
            await self.service.resume(self.tokens[1], run_id)
        self.assertEqual(self.runs.get(self.tokens[1], run_id)["status"], "interrupted")
        self.assertNotIn(self.tokens[1], self.service.requests.owners)

    async def test_cancelled_resume_returns_result_and_releases_session(self):
        run_id = self.make_resumable(0)

        async def graph(state, **kwargs):
            await self.entered.put(self.tokens[0])
            await self.release.wait()
            return state

        self.runtime.graph = SimpleNamespace(ainvoke=graph)
        resume = asyncio.create_task(self.service.resume(self.tokens[0], run_id))
        self.tasks.append(resume)
        await self.wait_entered()
        self.service.cancel(self.tokens[0], run_id)
        result = await resume
        self.assertEqual(result["task_status"], "cancelled")
        self.assertTrue(result["degraded"])
        self.assertEqual(self.service.requests.active, 0)
        self.assertEqual(self.service.requests.owners, set())
        self.assertEqual(self.runs.get(self.tokens[0], run_id)["status"], "cancelled")

    async def test_programming_error_releases_capacity_and_marks_failed(self):
        async def broken(*args):
            raise KeyError("fixture")
        self.service.run = broken
        with self.assertRaises(KeyError):
            await self.service.chat(self.tokens[0], "fixture", "broken-request")
        self.assertEqual(self.service.requests.active, 0)
        self.assertEqual(self.service.requests.owners, set())
        self.assertEqual(self.runs.by_request_key(self.tokens[0], "broken-request")["status"], "failed")


class CPUQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cpu = CPUQueue(max_pending=2)
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = []

    async def asyncTearDown(self):
        self.release.set()
        await self.cpu.close()

    def blocked(self):
        self.started.set()
        self.release.wait(2)
        self.calls.append("old")

    async def start_blocked(self):
        task = asyncio.create_task(self.cpu.run(self.blocked))
        while not self.started.is_set():
            await asyncio.sleep(.001)
        return task

    async def test_full_queue_rejects_and_waiting_work_is_serial(self):
        first = await self.start_blocked()
        second = asyncio.create_task(self.cpu.run(lambda: self.calls.append("new")))
        await asyncio.sleep(.01)
        with self.assertRaisesRegex(RuntimeError, "queue_full"):
            await self.cpu.run(lambda: None)
        self.assertEqual(self.calls, [])
        self.release.set()
        await asyncio.gather(first, second)
        self.assertEqual(self.calls, ["old", "new"])
        self.assertEqual(self.cpu.pending, 0)

    async def test_cancelled_waiter_never_starts(self):
        first = await self.start_blocked()
        waiting = asyncio.create_task(self.cpu.run(lambda: self.calls.append("cancelled")))
        await asyncio.sleep(.01)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.release.set()
        await first
        self.assertEqual(self.calls, ["old"])

    async def test_cancelled_running_thread_is_not_overlapped(self):
        first = await self.start_blocked()
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(self.cpu.run(lambda: self.calls.append("new")))
        await asyncio.sleep(.01)
        self.assertFalse(second.done())
        self.assertTrue(self.cpu.busy)
        self.release.set()
        await second
        self.assertEqual(self.calls, ["old", "new"])

    async def test_wait_timeout_does_not_execute_late(self):
        first = await self.start_blocked()
        self.cpu.timeout = .01
        with self.assertRaises(TimeoutError):
            await self.cpu.run(lambda: self.calls.append("too-late"))
        self.release.set()
        await first
        self.assertEqual(self.calls, ["old"])

    async def test_closed_queue_rejects_new_work(self):
        await self.cpu.close()
        with self.assertRaisesRegex(RuntimeError, "queue_closed"):
            await self.cpu.run(lambda: None)
