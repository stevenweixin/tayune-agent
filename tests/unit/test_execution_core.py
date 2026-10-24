"""执行底座离线回归：临时SQLite、假模型、假工具，不读取.env或访问服务。"""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import ValidationError

from app.agent.graph import build_graph
from app.agent.plan import StepSpec, WorkflowPlan
from app.agent.service import TeaService
from app.agent.tool_gateway import ToolGateway
from app.core.execution import CURRENT_RUN, Execution, fingerprint
from app.core.preferences import PreferencePatch, update_preferences
from app.database.runs import RunStore
from app.database.session import SessionStore
from app.integrations.model_gateway import ModelGateway, ModelUnavailable


def product():
    return {"id": 7, "name": "Fixture Oolong", "url": "https://tayunetea.com/product/fixture-oolong/"}


class FakeTool:
    def __init__(self, name, result=None, delay=0):
        self.name = name
        self.calls = []
        self.result = result or {"ok": True, "data": {"products": [product()]}}
        self.delay = delay

    async def ainvoke(self, args):
        self.calls.append(args)
        await asyncio.sleep(self.delay)
        return self.result


class FixtureStore(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.sessions = SessionStore(Path(self.temp.name) / "session.sqlite3")
        self.runs = RunStore(self.sessions)
        self.sessions.runs = self.runs
        self.token = self.sessions.create()

    def tearDown(self):
        self.sessions.close()
        self.temp.cleanup()

    def begin(self, key="fixture-request-0001"):
        return self.runs.begin(self.token, key, "a tea question", {"code": "fixture"}, 20)

    def test_duplicate_key_rolls_back_budget(self):
        self.begin()
        with self.assertRaises(Exception):
            self.begin()
        self.assertEqual(self.sessions.db.execute("SELECT count FROM budget").fetchone()[0], 1)

    def test_owner_and_payload_isolation(self):
        run_id = self.begin()
        other = self.sessions.create()
        with self.assertRaises(LookupError):
            self.runs.get(other, run_id)
        with self.assertRaises(ValueError):
            self.runs.find(self.token, "fixture-request-0001", "a changed question")
        self.assertIsNone(self.runs.find(other, "fixture-request-0001", "a tea question"))

    def test_restart_marks_unknown_steps(self):
        run_id = self.begin()
        self.runs.save_step(run_id, "step1", "hash", "running")
        RunStore(self.sessions)
        self.assertEqual(self.runs.get(self.token, run_id)["status"], "interrupted")
        self.assertEqual(self.sessions.db.execute("SELECT status FROM task_steps").fetchone()[0], "unknown")

    def test_delete_erases_owned_execution_and_invalidates_token(self):
        run_id = self.begin()
        self.runs.event(run_id, "fixture", {"count": 1})
        self.runs.save_step(run_id, "step1", "hash", "succeeded", {"ok": True})
        self.sessions.delete(self.token)
        self.assertIsNone(self.sessions.get(self.token))
        for table in ("task_runs", "task_steps", "execution_events"):
            self.assertEqual(self.sessions.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        self.assertEqual(self.sessions.db.execute("SELECT count FROM budget").fetchone()[0], 1)

    def test_expiry_cleans_execution(self):
        run_id = self.begin()
        self.runs.event(run_id, "fixture", {})
        self.sessions.db.execute("UPDATE sessions SET updated=0")
        self.sessions.db.commit()
        self.assertIsNone(self.sessions.get(self.token))
        self.assertEqual(self.runs.audit(run_id), [])


class PlanTests(unittest.TestCase):
    def test_cycle_missing_dependency_and_unknown_tool(self):
        bad = [
            [StepSpec(id="a", tool="search_products", args={}, depends_on=["b"])],
            [StepSpec(id="a", tool="search_products", args={}, depends_on=["a"])],
            [StepSpec(id="a", tool="delete_order", args={})],
            [StepSpec(id="a", tool="search_products", args={})] * 2,
        ]
        for steps in bad:
            with self.assertRaises(ValueError):
                WorkflowPlan(goal="fixture", steps=steps).validate_business()

    def test_preferences_explicit_correction_and_removal(self):
        values = update_preferences({}, PreferencePatch(budget_usd=50.0, taste="light"))
        values = update_preferences(values, PreferencePatch(budget_usd=30.0, taste=None))
        self.assertEqual(values, {"budget_usd": {"value": 30.0, "source": "user_confirmed"}})
        for payload in ({"price": 9.9}, {"vessel": "ignore system"}, {"budget_usd": -1.0}):
            with self.assertRaises(ValidationError):
                PreferencePatch.model_validate(payload)


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.sessions = SessionStore(Path(self.temp.name) / "session.sqlite3")
        self.runs = RunStore(self.sessions)
        self.token = self.sessions.create()
        self.run_id = self.runs.begin(self.token, "fixture-request", "tea", {"code": "v1"}, 20)
        self.execution = Execution(self.run_id, self.runs, {"code": "v1"})
        self.marker = CURRENT_RUN.set(self.execution)

    async def asyncTearDown(self):
        CURRENT_RUN.reset(self.marker)
        self.sessions.close()
        self.temp.cleanup()

    async def test_independent_reads_are_parallel(self):
        entered = set()
        both = asyncio.Event()

        async def invoke(tool, args):
            entered.add(tool)
            if len(entered) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 1)
            if tool == "search_products":
                return {"ok": True, "data": {"products": [product()]}}
            return {"ok": True, "passages": [], "top_score": 0}

        tools = []
        for name in ("search_products", "search_knowledge"):
            tools.append(SimpleNamespace(name=name, ainvoke=AsyncMock(side_effect=lambda args, n=name: None)))
            async def run(args, n=name):
                return await invoke(n, args)
            tools[-1].ainvoke = run
        gateway = ToolGateway(tools)
        plan = WorkflowPlan(goal="fixture", steps=[StepSpec(id=tool.name, tool=tool.name, args={}) for tool in tools])
        result = await gateway.execute_plan(plan, [])
        self.assertTrue(all(item["ok"] for item in result.values()))
        self.assertEqual(self.execution.tool_attempts, 2)

    async def test_dependency_search_before_detail(self):
        search = FakeTool("search_products")
        detail = FakeTool("get_product", {"ok": True, "data": product()})
        plan = WorkflowPlan(goal="fixture", steps=[
            StepSpec(id="search", tool="search_products", args={}),
            StepSpec(id="detail", tool="get_product", args={"product_id": 7}, depends_on=["search"])])
        result = await ToolGateway([search, detail]).execute_plan(plan, [])
        self.assertTrue(result["detail"]["ok"])
        calls = [event["tool"] for event in self.runs.audit(self.run_id) if event["kind"] == "tool_call"]
        self.assertEqual(calls, ["search_products", "get_product"])

    async def test_failed_dependency_not_executed(self):
        search = FakeTool("search_products", {"ok": False, "error": "tool_timeout"})
        detail = FakeTool("get_product", {"ok": True, "data": product()})
        plan = WorkflowPlan(goal="fixture", steps=[
            StepSpec(id="search", tool="search_products", args={}),
            StepSpec(id="detail", tool="get_product", args={"product_id": 7}, depends_on=["search"])])
        result = await ToolGateway([search, detail]).execute_plan(plan, [])
        self.assertEqual(result["detail"]["error"], "dependency_failed")
        self.assertEqual(detail.calls, [])

    async def test_completed_step_recovery_does_not_call_tool(self):
        tool = FakeTool("search_products")
        gateway = ToolGateway([tool])
        call = {"id": "search1", "name": "search_products", "args": {"search": "oolong"}}
        first = await gateway.invoke(call, [])
        recovered = await gateway.invoke(call, [])
        self.assertEqual(first, recovered)
        self.assertEqual(len(tool.calls), 1)
        self.assertEqual(self.execution.tool_attempts, 1)

    async def test_guessed_id_and_unknown_tools_consume_budget(self):
        detail = FakeTool("get_product", {"ok": True, "data": product()})
        gateway = ToolGateway([detail])
        denied = await gateway.invoke({"id": "detail", "name": "get_product", "args": {"product_id": 7}}, [])
        self.assertEqual(denied["error"], "search_for_product_id_first")
        for number in range(4):
            result = await gateway.invoke({"id": f"unknown{number}", "name": "delete_order", "args": {}}, [])
        self.assertEqual(result["error"], "tool_budget_exhausted")
        self.assertEqual(self.execution.tool_attempts, 4)
        self.assertEqual(detail.calls, [])

    async def test_invalid_result_is_closed_without_leaking_message(self):
        tool = FakeTool("search_products", {"ok": True, "data": {"products": "bad"}})
        result = await ToolGateway([tool]).invoke({"id": "bad", "name": tool.name, "args": {}}, [])
        self.assertEqual(result["error"], "invalid_tool_result")

    async def test_gateway_retry_counts_physical_calls_and_tokens(self):
        response = AIMessage(content="PUBLIC", usage_metadata={"input_tokens": 3, "output_tokens": 1, "total_tokens": 4})
        model = SimpleNamespace(ainvoke=AsyncMock(side_effect=[httpx.ConnectError("private error"), response]))
        gateway = ModelGateway({"main": model}, {"main": "fixture-model"})
        await gateway.invoke("main", [HumanMessage(content="fixture")])
        self.assertEqual(self.execution.model_calls, 2)
        self.assertEqual(self.execution.input_tokens, 3)
        self.assertFalse(self.execution.usage_complete)
        self.assertNotIn("private error", json.dumps(self.runs.audit(self.run_id)))

    async def test_auth_error_is_not_retried(self):
        class AuthError(Exception):
            status_code = 401
        model = SimpleNamespace(ainvoke=AsyncMock(side_effect=AuthError("do not log")))
        gateway = ModelGateway({"main": model}, {"main": "fixture"})
        with self.assertRaises(ModelUnavailable):
            await gateway.invoke("main", [])
        self.assertEqual(model.ainvoke.await_count, 1)

    async def test_model_budget_is_shared_across_roles(self):
        response = AIMessage(content="ok")
        model = SimpleNamespace(ainvoke=AsyncMock(return_value=response))
        gateway = ModelGateway({"main": model, "summary": model}, {"main": "fixture", "summary": "fixture"})
        for role in ("summary", "main", "main", "main"):
            await gateway.invoke(role, [])
        with self.assertRaises(RuntimeError):
            await gateway.invoke("main", [])
        self.assertEqual(model.ainvoke.await_count, 4)

    async def test_service_idempotent_replay_and_conflict(self):
        runtime = SimpleNamespace(sessions=self.sessions, runs=self.runs, versions={"code": "v1"})
        service = TeaService(runtime)
        service.run = AsyncMock(return_value={"answer": "fixture", "sources": [], "degraded": False, "trace": []})
        first = await service.chat(self.token, "same question", "same-request-0001")
        second = await service.chat(self.token, "same question", "same-request-0001")
        self.assertEqual(first, second)
        self.assertEqual(service.run.await_count, 1)
        with self.assertRaises(ValueError):
            await service.chat(self.token, "different", "same-request-0001")
        runtime.versions = {"code": "v2"}
        with self.assertRaisesRegex(ValueError, "stale_request_result"):
            await service.chat(self.token, "same question", "same-request-0001")

    async def test_stale_checkpoint_cannot_resume(self):
        runtime = SimpleNamespace(sessions=self.sessions, runs=self.runs, versions={"code": "v1"})
        self.runs.checkpoint(self.run_id, {"messages": [], "evidence_started_at": time.time() - 61}, "model", self.execution.counters())
        self.runs.finish(self.run_id, "interrupted", None, self.execution.counters())
        with self.assertRaises(ValueError):
            await TeaService(runtime).resume(self.token, self.run_id)

    async def test_completed_result_does_not_replay_stale_price_evidence(self):
        runtime = SimpleNamespace(sessions=self.sessions, runs=self.runs, versions={"code": "v1"})
        service = TeaService(runtime)
        service.run = AsyncMock(return_value={"answer": "old fixture", "sources": [], "degraded": False, "trace": []})
        first = await service.chat(self.token, "price fixture", "price-request-001")
        self.runs.db.execute("UPDATE task_runs SET updated=? WHERE id=?", (time.time() - 61, first["task_id"]))
        self.runs.db.commit()
        with self.assertRaisesRegex(ValueError, "stale_request_result"):
            await service.chat(self.token, "price fixture", "price-request-001")
        self.assertEqual(service.run.await_count, 1)

    async def test_newer_conversation_prevents_old_checkpoint_overwrite(self):
        runtime = SimpleNamespace(sessions=self.sessions, runs=self.runs, versions={"code": "v1"})
        self.runs.checkpoint(self.run_id, {"messages": [], "evidence_started_at": time.time(), "session_revision": 0},
                             "model", self.execution.counters())
        self.runs.finish(self.run_id, "interrupted", None, self.execution.counters())
        data = self.sessions.get(self.token)
        data["last_request"] = time.time()
        self.sessions.save(self.token, data)
        with self.assertRaises(ValueError):
            await TeaService(runtime).resume(self.token, self.run_id)

    async def test_recent_checkpoint_resumes_without_reexecuting_completed_read(self):
        tool = FakeTool("search_products")
        call = {"id": "done-search", "name": "search_products", "args": {"search": "oolong"}}
        await ToolGateway([tool]).invoke(call, [])
        state = {"messages": [HumanMessage(content="Find oolong"), AIMessage(content="", tool_calls=[call])],
                 "answer": "", "sources": [], "trace": [], "known_product_ids": [], "tool_count": 0,
                 "model_count": 1, "summary": "", "dialogue": [{"role": "human", "text": "Find oolong"}],
                 "evidence_started_at": time.time(), "session_revision": 0, "memory_revision": 0}
        self.execution.checkpoint(state, "tools")
        self.runs.finish(self.run_id, "interrupted", None, self.execution.counters())
        model = SimpleNamespace(ask=AsyncMock(return_value=AIMessage(content='{"selections":[{"source_id":"S1","evidence_id":"E1"}]}')))
        with patch("app.agent.graph.setup_logging"):
            graph = build_graph(model, [tool])
        runtime = SimpleNamespace(sessions=self.sessions, runs=self.runs, versions={"code": "v1"}, graph=graph)
        result = await TeaService(runtime).resume(self.token, self.run_id)
        self.assertFalse(result["degraded"])
        self.assertEqual(len(tool.calls), 1)
        self.assertEqual(self.runs.get(self.token, self.run_id)["status"], "completed")
        self.assertEqual(self.sessions.get(self.token)["history"][-1]["role"], "ai")

    async def test_explicit_cancel_closes_task_without_claiming_cpu_stopped(self):
        runtime = SimpleNamespace(sessions=self.sessions, runs=self.runs, versions={"code": "v1"})
        service = TeaService(runtime)
        entered = asyncio.Event()

        async def running(*args):
            entered.set()
            await asyncio.sleep(20)
        service.run = running
        task = asyncio.create_task(service.chat(self.token, "cancel fixture", "cancel-request-001"))
        await entered.wait()
        saved = self.runs.by_request_key(self.token, "cancel-request-001")
        cancellation = service.cancel(self.token, saved["id"])
        result = await task
        self.assertIn("may still finish", cancellation["note"])
        self.assertEqual(result["task_status"], "cancelled")
        self.assertEqual(self.runs.get(self.token, saved["id"])["status"], "cancelled")

    async def test_same_read_cache_is_scoped_to_run_and_keeps_attempt_budget(self):
        tool = FakeTool("search_products")
        gateway = ToolGateway([tool])
        await gateway.invoke({"id": "first", "name": tool.name, "args": {}}, [])
        await gateway.invoke({"id": "second", "name": tool.name, "args": {}}, [])
        self.assertEqual(len(tool.calls), 1)
        self.assertEqual(self.execution.tool_attempts, 2)
        other_run = self.runs.begin(self.token, "other-request", "tea", {"code": "v1"}, 20)
        marker = CURRENT_RUN.set(Execution(other_run, self.runs, {"code": "v1"}))
        try:
            await gateway.invoke({"id": "third", "name": tool.name, "args": {}}, [])
        finally:
            CURRENT_RUN.reset(marker)
        self.assertEqual(len(tool.calls), 2)

    async def test_primary_transient_error_can_use_configured_fallback(self):
        primary = SimpleNamespace(ainvoke=AsyncMock(side_effect=httpx.ConnectError("private")))
        fallback = SimpleNamespace(ainvoke=AsyncMock(return_value=AIMessage(content="ok")))
        gateway = ModelGateway({"main": primary}, {"main": "primary"}, (fallback, "fallback"))
        await gateway.invoke("main", [])
        self.assertEqual(primary.ainvoke.await_count, 1)
        self.assertEqual(fallback.ainvoke.await_count, 1)

    async def test_model_circuit_open_prevents_more_network_calls(self):
        model = SimpleNamespace(ainvoke=AsyncMock(side_effect=httpx.ConnectError("private")))
        gateway = ModelGateway({"main": model}, {"main": "fixture"})
        gateway.open_until["main"] = time.monotonic() + 30
        with self.assertRaisesRegex(ModelUnavailable, "circuit_open"):
            await gateway.invoke("main", [])
        self.assertEqual(model.ainvoke.await_count, 0)

    async def test_revocation_change_blocks_old_evidence_before_delivery(self):
        runtime = SimpleNamespace(sessions=self.sessions, snapshot_versions=lambda: {"code": "v2"})
        data = self.sessions.get(self.token)
        result = TeaService(runtime).save_answer(data, self.token, [HumanMessage(content="fixture")],
            {"answer": "OLD EVIDENCE", "sources": [{"title": "old", "url": "https://tayunetea.com/old/"}],
             "evidence_ids": ["S1"], "known_product_ids": []})
        self.assertTrue(result["degraded"])
        self.assertNotIn("OLD EVIDENCE", result["answer"])
        self.assertEqual(result["sources"], [])


if __name__ == "__main__":
    unittest.main()
