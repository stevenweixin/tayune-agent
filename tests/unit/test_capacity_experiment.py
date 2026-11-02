"""只验证容量实验注入层，正式Service和CPUWorker默认行为不变。"""
import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.database.runs import RunStore
from app.database.session import SessionStore
from tests.performance.capacity_runtime import CapacityGate, CapacityService, QueuedCPU
from tests.performance.capacity_server import SharedBudget


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_capacity_has_no_unbounded_wait_queue(self):
        gate = CapacityGate(2)
        async with gate:
            self.assertFalse(gate.locked())
            async with gate:
                self.assertTrue(gate.locked())
                with self.assertRaises(RuntimeError):
                    async with gate:
                        pass
        self.assertEqual(gate.active, 0)
        self.assertEqual(gate.peak, 2)

    async def test_exception_releases_slot(self):
        gate = CapacityGate(1)
        with self.assertRaises(ValueError):
            async with gate:
                raise ValueError("fixture")
        self.assertFalse(gate.locked())

    async def test_same_owner_is_rejected_and_different_owner_can_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "sessions.sqlite3")
            runtime = SimpleNamespace(sessions=store, runs=RunStore(store), versions={}, output=Path(directory))
            service = CapacityService(runtime, 2)
            entered = asyncio.Event()
            release = asyncio.Event()

            async def run(*args):
                entered.set()
                await release.wait()
                return {"answer": "fixture", "sources": [], "degraded": False}

            service.run = AsyncMock(side_effect=run)
            a, b = store.create(), store.create()
            first = asyncio.create_task(service.chat(a, "first", "request-first-001"))
            await entered.wait()
            try:
                with self.assertRaisesRegex(RuntimeError, "session_busy"):
                    await service.chat(a, "second", "request-second-001")
                second = asyncio.create_task(service.chat(b, "other", "request-other-001"))
                await asyncio.sleep(.01)
                self.assertEqual(service.lock.peak, 2)
                release.set()
                results = await asyncio.gather(first, second)
                self.assertNotEqual(results[0]["task_id"], results[1]["task_id"])
                self.assertEqual(store.db.execute("SELECT count FROM budget").fetchone()[0], 2)
                replay = await service.chat(a, "first", "request-first-001")
                self.assertEqual(replay["task_id"], results[0]["task_id"])
                self.assertEqual(service.run.await_count, 2)
            finally:
                release.set()
                await first
                store.close()


class QueuedCPUTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_is_bounded_and_serial(self):
        cpu = QueuedCPU(max_pending=2)
        started, release = threading.Event(), threading.Event()
        calls = []

        def operation(name):
            calls.append(name)
            started.set()
            if name == "a":
                release.wait(2)
            return name

        first = asyncio.create_task(cpu.run(operation, "a"))
        while not started.is_set():
            await asyncio.sleep(.001)
        second = asyncio.create_task(cpu.run(operation, "b"))
        await asyncio.sleep(.01)
        try:
            with self.assertRaisesRegex(RuntimeError, "queue_full"):
                await cpu.run(operation, "c")
            self.assertEqual(calls, ["a"])
            release.set()
            self.assertEqual(await asyncio.gather(first, second), ["a", "b"])
            self.assertEqual(cpu.peak_pending, 2)
        finally:
            release.set()
            await cpu.close()

    async def test_cancel_does_not_allow_second_cpu_thread_to_overlap(self):
        cpu = QueuedCPU(max_pending=2)
        started, release = threading.Event(), threading.Event()
        executed = []

        def old():
            started.set()
            release.wait(2)
            executed.append("old")

        first = asyncio.create_task(cpu.run(old))
        while not started.is_set():
            await asyncio.sleep(.001)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        next_task = asyncio.create_task(cpu.run(lambda: executed.append("new")))
        try:
            await asyncio.sleep(.01)
            self.assertEqual(executed, [])
            self.assertFalse(next_task.done())
            release.set()
            await next_task
            self.assertEqual(executed, ["old", "new"])
        finally:
            release.set()
            await cpu.close()


class BudgetTests(unittest.TestCase):
    def test_inflight_cost_is_reserved_before_network_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            budget = SharedBudget(Path(directory) / "usage.jsonl", max_usd=.12)
            budget.reserve()
            budget.reserve()
            with self.assertRaises(RuntimeError):
                budget.reserve()
            self.assertEqual(budget.attempts, 2)
            self.assertAlmostEqual(budget.reserved_usd, .1)


if __name__ == "__main__":
    unittest.main()
