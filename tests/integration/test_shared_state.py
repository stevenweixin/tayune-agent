"""独立真实PostgreSQL、多连接和多进程契约；默认跳过，绝不接触正式数据库。"""
import asyncio
import json
import os
import sys
import unittest

from app.database.shared_state import SharedState, SharedStateError, digest
from tests.performance.shared_state_worker import require_test_dsn


@unittest.skipUnless(os.environ.get("TAYUNE_STATE_TEST_DSN"), "Dedicated PostgreSQL test database required")
class SharedStateContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dsn = require_test_dsn()
        self.a = await SharedState.connect(self.dsn, capacity=4, max_sessions=5000, lease_seconds=2)
        self.b = await SharedState.connect(self.dsn, capacity=4, max_sessions=5000, lease_seconds=2)
        self.workers = []
        async with self.a.transaction() as connection:
            # DSN已严格限制；只清理此测试库ta_表，不删除容器/其他项目的数据。
            # DELETE触发ON DELETE行为；TRUNCATE CASCADE会连名额配置行也清空。
            await connection.execute("DELETE FROM ta_sessions")
            await connection.execute("DELETE FROM ta_budget")
            await connection.execute("UPDATE ta_slots SET session_id=NULL,holder=NULL,fence=NULL,lease_until=NULL")

    async def asyncTearDown(self):
        for process in self.workers:
            if process.returncode is None:
                process.kill()
            await process.wait()
        await self.b.close()
        await self.a.close()

    async def start(self, store=None):
        store = store or self.a
        token = await store.create_session()
        lease = await store.acquire_session(token)
        result = await store.start_run(lease, "fixture-request-0001", "tea question", {"code": "fixture"}, daily_limit=100)
        return token, lease, result

    async def scalar(self, sql, *args):
        async with self.a.pool.acquire() as connection:
            return await connection.fetchval(sql, *args)

    async def edit(self, sql, *args):
        async with self.a.transaction() as connection:
            await connection.execute(sql, *args)

    async def child(self, token, key):
        process = await asyncio.create_subprocess_exec(sys.executable, "-m", "tests.performance.shared_state_worker",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self.workers.append(process)
        process.stdin.write((json.dumps({"token": token, "key": key}) + "\n").encode())
        await process.stdin.drain()
        ready = await self.read_child(process)
        self.assertEqual(ready["state"], "ready")
        return process

    async def read_child(self, process):
        line = await asyncio.wait_for(process.stdout.readline(), 10)
        if not line:
            # 不输出原始stderr，避免连接失败时泄漏数据库凭据。
            self.fail("State worker exited before its expected response")
        return json.loads(line)

    async def tell_child(self, process, message="go"):
        process.stdin.write((message + "\n").encode())
        await process.stdin.drain()

    async def test_session_shared_and_token_not_stored(self):
        token = await self.a.create_session()
        self.assertEqual((await self.b.get_session(token))["data"]["history"], [])
        self.assertEqual(await self.scalar("SELECT id FROM ta_sessions"), digest(token))
        self.assertNotEqual(token, digest(token))
        self.assertIsNone(await self.b.get_session("not-a-session"))

    async def test_second_instance_does_not_interrupt_running(self):
        token, lease, result = await self.start()
        extra = await SharedState.connect(self.dsn, capacity=4, max_sessions=5000, lease_seconds=2)
        try:
            self.assertEqual((await extra.inspect_run(token, result["run"]["id"]))["status"], "running")
            with self.assertRaisesRegex(SharedStateError, "session_busy"):
                await extra.acquire_session(token)
        finally:
            await extra.close()
            await self.a.release(lease)

    async def test_conflicting_cluster_settings_rejected(self):
        with self.assertRaisesRegex(SharedStateError, "shared_settings_mismatch"):
            await SharedState.connect(self.dsn, capacity=5, max_sessions=5000, lease_seconds=2)

    async def test_one_session_has_only_one_owner(self):
        token = await self.a.create_session()
        results = await asyncio.gather(self.a.acquire_session(token), self.b.acquire_session(token), return_exceptions=True)
        winners = [item for item in results if not isinstance(item, Exception)]
        self.assertEqual(len(winners), 1)
        self.assertEqual(sum(str(item) == "session_busy" for item in results), 1)
        await self.a.release(winners[0])

    async def test_expired_owner_fenced_from_all_writes(self):
        token, old, started = await self.start()
        run_id = started["run"]["id"]
        await self.a.checkpoint(old, run_id, {"private": "fixture"}, "model", {"model_calls": 1})
        await self.a.record_step(old, run_id, "step-1", "hash-1", "running")
        await self.edit("UPDATE ta_sessions SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1", old.session_id)
        new = await self.b.acquire_session(token)
        self.assertGreater(new.fence, old.fence)
        self.assertEqual((await self.b.inspect_run(token, run_id))["status"], "interrupted")
        self.assertEqual(await self.scalar("SELECT status FROM ta_steps WHERE run_id=$1", run_id), "unknown")
        operations = [self.a.renew(old), self.a.save_session(old, {"history": ["stale"]}, revision=1),
            self.a.checkpoint(old, run_id, {}, "__end__", {}),
            self.a.record_event(old, run_id, "stale", {}), self.a.record_step(old, run_id, "s", "h", "succeeded", {}),
            self.a.finish(old, run_id, "completed", {"answer": "stale"}, {}), self.a.delete_session(old)]
        for operation in operations:
            with self.assertRaisesRegex(SharedStateError, "lease_lost"):
                await operation
        self.assertFalse(await self.a.release(old))
        self.assertEqual(await self.scalar("SELECT holder FROM ta_sessions WHERE id=$1", new.session_id), new.holder)
        self.assertEqual((await self.b.inspect_run(token, run_id))["checkpoint"], {"private": "fixture"})
        await self.b.release(new)

    async def test_renew_keeps_same_owner_and_slot_alive(self):
        token, lease, started = await self.start()
        before = await self.scalar("SELECT lease_until FROM ta_sessions WHERE id=$1", lease.session_id)
        await asyncio.sleep(.03)
        renewed = await self.b.renew(lease)
        self.assertGreater(renewed["lease_until"], before)
        self.assertEqual(await self.scalar("SELECT lease_until FROM ta_slots WHERE session_id=$1", lease.session_id), renewed["lease_until"])
        with self.assertRaisesRegex(SharedStateError, "session_busy"):
            await self.b.acquire_session(token)
        await self.a.finish(lease, started["run"]["id"], "completed", {}, {})
        await self.a.release(lease)

    async def test_global_slots_are_not_per_instance(self):
        held = [await self.start(self.a if index % 2 else self.b) for index in range(4)]
        token = await self.b.create_session()
        lease = await self.b.acquire_session(token)
        with self.assertRaisesRegex(SharedStateError, "capacity_full"):
            await self.b.start_run(lease, "capacity-request-5", "tea", {}, daily_limit=100)
        self.assertEqual(await self.scalar("SELECT sum(count) FROM ta_budget"), 4)
        self.assertEqual(await self.scalar("SELECT count(*) FROM ta_runs"), 4)
        self.assertEqual((await self.b.get_session(token))["data"]["last_request"], 0)
        await self.b.release(lease)
        for _, owner, result in held:
            await self.a.finish(owner, result["run"]["id"], "completed", {}, {})
            await self.a.release(owner)
        self.assertEqual(await self.scalar("SELECT count(*) FROM ta_slots WHERE holder IS NOT NULL"), 0)

    async def test_lost_execution_slot_cannot_be_renewed_or_committed(self):
        token, old, started = await self.start()
        await self.edit("UPDATE ta_slots SET lease_until=clock_timestamp()-interval '1 second' WHERE session_id=$1", old.session_id)
        other, new, _ = await self.start(self.b)
        with self.assertRaisesRegex(SharedStateError, "execution_slot_lost"):
            await self.a.renew(old)
        with self.assertRaisesRegex(SharedStateError, "execution_slot_lost"):
            await self.a.finish(old, started["run"]["id"], "completed", {"answer": "stale"}, {})
        self.assertTrue(await self.a.release(old))
        self.assertEqual(await self.scalar("SELECT count(*) FROM ta_slots WHERE holder=$1 AND fence=$2", new.holder, new.fence), 1)
        await self.b.release(new)

    async def test_session_capacity_is_atomic_and_expired_rows_do_not_count(self):
        # 在专属库把两连接的同一配置降至2，验证原子上限；退出前恢复配置。
        await self.edit("UPDATE ta_settings SET max_sessions=2")
        self.a.max_sessions = self.b.max_sessions = 2
        try:
            results = await asyncio.gather(self.a.create_session(), self.b.create_session(), self.a.create_session(), return_exceptions=True)
            tokens = [item for item in results if isinstance(item, str)]
            self.assertEqual(len(tokens), 2)
            self.assertEqual(sum(str(item) == "session_capacity" for item in results), 1)
            await self.edit("UPDATE ta_sessions SET expires_at=clock_timestamp()-interval '1 second' WHERE id=$1", digest(tokens[0]))
            extra = await self.b.create_session()
            self.assertIsNotNone(await self.a.get_session(extra))
            self.assertEqual(await self.scalar("SELECT count(*) FROM ta_sessions"), 3)
        finally:
            self.a.max_sessions = self.b.max_sessions = 5000
            await self.edit("UPDATE ta_settings SET max_sessions=5000")

    async def test_quota_and_registration_are_one_transaction(self):
        owners = []
        for index in range(3):
            token = await self.a.create_session()
            lease = await self.a.acquire_session(token)
            owners.append((token, lease))
        async def attempt(lease, index):
            try:
                return await self.b.start_run(lease, f"quota-request-{index:04d}", "tea", {}, daily_limit=2)
            except SharedStateError as error:
                return str(error)
        results = await asyncio.gather(*(attempt(lease, index) for index, (_, lease) in enumerate(owners)))
        self.assertEqual(sum(isinstance(row, dict) for row in results), 2)
        self.assertEqual(results.count("daily_limit"), 1)
        self.assertEqual(await self.scalar("SELECT sum(count) FROM ta_budget"), 2)
        self.assertEqual(await self.scalar("SELECT count(*) FROM ta_runs"), 2)
        self.assertEqual(await self.scalar("SELECT count(*) FROM ta_slots WHERE holder IS NOT NULL"), 2)
        for _, lease in owners:
            await self.a.release(lease)

    async def test_replay_and_conflict_across_instances_do_not_charge(self):
        token, lease, result = await self.start()
        run_id = result["run"]["id"]
        data = result["session"]["data"]
        data["history"] = [{"role": "ai", "text": "fixture answer"}]
        await self.a.finish(lease, run_id, "completed", {"answer": "fixture answer"}, {"model_calls": 1},
                            data=data, revision=result["session"]["revision"])
        await self.a.release(lease)
        new = await self.b.acquire_session(token)
        replay = await self.b.start_run(new, "fixture-request-0001", "tea question", {"code": "fixture"}, daily_limit=100)
        self.assertFalse(replay["created"])
        self.assertEqual(replay["run"]["id"], run_id)
        self.assertEqual(await self.scalar("SELECT sum(count) FROM ta_budget"), 1)
        with self.assertRaisesRegex(SharedStateError, "idempotency_conflict"):
            await self.b.start_run(new, "fixture-request-0001", "other question", {"code": "fixture"})
        with self.assertRaisesRegex(SharedStateError, "stale_request_result"):
            await self.b.start_run(new, "fixture-request-0001", "tea question", {"code": "changed"})
        await self.b.release(new)

    async def test_stale_evidence_is_not_replayed(self):
        token, lease, result = await self.start()
        run_id = result["run"]["id"]
        await self.a.checkpoint(lease, run_id, {"evidence_started_at": 1}, "__end__", {})
        await self.a.finish(lease, run_id, "completed", {"answer": "expired"}, {})
        await self.a.release(lease)
        new = await self.b.acquire_session(token)
        with self.assertRaisesRegex(SharedStateError, "stale_request_result"):
            await self.b.start_run(new, "fixture-request-0001", "tea question", {"code": "fixture"})
        await self.b.release(new)

    async def test_revision_conflict_rolls_back_final_result_and_slot_release(self):
        token, lease, result = await self.start()
        run_id = result["run"]["id"]
        with self.assertRaisesRegex(SharedStateError, "session_revision_conflict"):
            await self.b.finish(lease, run_id, "completed", {"answer": "bad"}, {}, data={"history": []}, revision=0)
        self.assertEqual((await self.a.inspect_run(token, run_id))["status"], "running")
        self.assertEqual(await self.scalar("SELECT count(*) FROM ta_slots WHERE holder IS NOT NULL"), 1)
        await self.a.finish(lease, run_id, "completed", {}, {}, data=result["session"]["data"], revision=1)
        self.assertEqual((await self.b.get_session(token))["revision"], 2)
        await self.a.release(lease)

    async def test_step_replay_and_payload_conflict(self):
        _, lease, result = await self.start()
        run_id = result["run"]["id"]
        await self.a.record_step(lease, run_id, "tool-1", "hash", "succeeded", {"ok": True})
        cached = await self.b.record_step(lease, run_id, "tool-1", "hash", "running")
        self.assertEqual(cached, {"ok": True})
        with self.assertRaisesRegex(SharedStateError, "step_payload_changed"):
            await self.b.record_step(lease, run_id, "tool-1", "changed", "running")
        await self.a.release(lease)

    async def test_cancellation_is_shared_and_owner_checked(self):
        token, lease, result = await self.start()
        run_id = result["run"]["id"]
        other = await self.b.create_session()
        with self.assertRaisesRegex(SharedStateError, "not_owned"):
            await self.b.request_cancel(other, run_id)
        await self.b.request_cancel(token, run_id)
        self.assertTrue((await self.a.renew(lease))["cancel_requested"])
        with self.assertRaisesRegex(SharedStateError, "task_not_found"):
            await self.b.inspect_run(other, run_id)
        await self.a.finish(lease, run_id, "cancelled", None, {})
        await self.a.release(lease)

    async def test_delete_erases_runs_steps_events_not_daily_count(self):
        token, lease, result = await self.start()
        run_id = result["run"]["id"]
        await self.a.record_event(lease, run_id, "fixture", {"count": 1})
        await self.a.record_step(lease, run_id, "s", "h", "succeeded", {"text": "private fixture"})
        await self.a.delete_session(lease)
        self.assertIsNone(await self.b.get_session(token))
        for table in ("ta_runs", "ta_steps", "ta_events"):
            self.assertEqual(await self.scalar("SELECT count(*) FROM " + table), 0)
        self.assertEqual(await self.scalar("SELECT sum(count) FROM ta_budget"), 1)
        self.assertFalse(await self.b.release(lease))

    async def test_expired_session_not_read_or_acquired_and_cleanup_cascades(self):
        token, lease, _ = await self.start()
        await self.a.release(lease)
        await self.edit("UPDATE ta_sessions SET expires_at=clock_timestamp()-interval '1 second' WHERE id=$1", lease.session_id)
        self.assertIsNone(await self.b.get_session(token))
        with self.assertRaisesRegex(SharedStateError, "session_expired"):
            await self.b.acquire_session(token)
        self.assertEqual(await self.a.cleanup(batch_size=1), {"sessions": 1, "runs": 0})
        self.assertEqual(await self.scalar("SELECT count(*) FROM ta_runs"), 0)

    async def test_old_results_expire_even_when_session_stays_active(self):
        token, lease, result = await self.start()
        run_id = result["run"]["id"]
        await self.a.finish(lease, run_id, "completed", {"answer": "private fixture"}, {})
        await self.edit("UPDATE ta_runs SET updated_at=clock_timestamp()-interval '25 hours' WHERE id=$1", run_id)
        cleaned = await self.b.cleanup(batch_size=1)
        self.assertEqual(cleaned, {"sessions": 0, "runs": 1})
        self.assertIsNotNone(await self.a.get_session(token))
        await self.a.release(lease)

    async def test_active_running_task_is_not_deleted_by_cleanup(self):
        _, lease, result = await self.start()
        await self.edit("UPDATE ta_runs SET updated_at=clock_timestamp()-interval '25 hours' WHERE id=$1", result["run"]["id"])
        self.assertEqual(await self.b.cleanup(), {"sessions": 0, "runs": 0})
        self.assertEqual(await self.scalar("SELECT count(*) FROM ta_runs"), 1)
        await self.a.release(lease)

    async def test_expired_lease_cannot_be_revived_without_new_fence(self):
        token, lease, _ = await self.start()
        await self.edit("UPDATE ta_sessions SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1", lease.session_id)
        with self.assertRaisesRegex(SharedStateError, "lease_lost"):
            await self.a.renew(lease)
        next_lease = await self.b.acquire_session(token)
        self.assertGreater(next_lease.fence, lease.fence)
        await self.b.release(next_lease)

    async def test_cancelled_waiting_transaction_rolls_back_everything(self):
        token = await self.a.create_session()
        lease = await self.a.acquire_session(token)
        async with self.b.transaction() as blocker:
            await blocker.execute("INSERT INTO ta_budget VALUES((clock_timestamp() AT TIME ZONE 'UTC')::date,0)")
            task = asyncio.create_task(self.a.start_run(lease, "cancelled-txn-0001", "tea", {}, daily_limit=100))
            try:
                await asyncio.sleep(.08)
                self.assertFalse(task.done())
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(await self.scalar("SELECT sum(count) FROM ta_budget"), 0)
        self.assertEqual(await self.scalar("SELECT count(*) FROM ta_runs"), 0)
        self.assertEqual(await self.scalar("SELECT count(*) FROM ta_slots WHERE holder IS NOT NULL"), 0)
        self.assertEqual((await self.a.get_session(token))["data"]["last_request"], 0)
        await self.a.release(lease)

    async def test_eight_independent_processes_share_four_execution_slots(self):
        tokens = [await self.a.create_session() for _ in range(8)]
        workers = [await self.child(token, f"process-request-{index:04d}") for index, token in enumerate(tokens)]
        for process in workers:
            await self.tell_child(process)
        responses = await asyncio.gather(*(self.read_child(process) for process in workers))
        started = [process for process, row in zip(workers, responses) if row["state"] == "started"]
        self.assertEqual(len(started), 4)
        self.assertEqual(sum(row.get("code") == "capacity_full" for row in responses), 4)
        self.assertEqual(await self.scalar("SELECT count(*) FROM ta_slots WHERE lease_until>clock_timestamp()"), 4)
        self.assertEqual(await self.scalar("SELECT sum(count) FROM ta_budget"), 4)
        for process in started:
            await self.tell_child(process, "finish")
        finished = await asyncio.gather(*(self.read_child(process) for process in started))
        self.assertTrue(all(row["state"] == "completed" for row in finished))
        for process in workers:
            self.assertEqual(await asyncio.wait_for(process.wait(), 5), 0)
        self.assertEqual(await self.scalar("SELECT count(*) FROM ta_slots WHERE holder IS NOT NULL"), 0)
        self.assertEqual(await self.scalar("SELECT count(*) FROM ta_sessions WHERE holder IS NOT NULL"), 0)

    async def test_process_crash_requires_expiry_then_fenced_takeover(self):
        token = await self.a.create_session()
        worker = await self.child(token, "crash-request-0001")
        await self.tell_child(worker)
        started = await self.read_child(worker)
        self.assertEqual(started["state"], "started")
        worker.kill()
        await worker.wait()
        with self.assertRaisesRegex(SharedStateError, "session_busy"):
            await self.b.acquire_session(token)
        await asyncio.sleep(2.05)
        lease = await self.b.acquire_session(token)
        self.assertGreater(lease.fence, started["fence"])
        recovered = await self.b.inspect_run(token, started["run_id"])
        self.assertEqual(recovered["status"], "interrupted")
        self.assertEqual(recovered["counters"]["model_calls"], 1)
        self.assertEqual(await self.scalar("SELECT status FROM ta_steps WHERE run_id=$1", started["run_id"]), "unknown")
        self.assertEqual(await self.scalar("SELECT sum(count) FROM ta_budget"), 1)
        with self.assertRaisesRegex(SharedStateError, "task_requires_recovery"):
            await self.b.start_run(lease, "crash-request-0001", "fixture tea question", {"code": "fixture"})
        await self.b.release(lease)


if __name__ == "__main__":
    unittest.main()
