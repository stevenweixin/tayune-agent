"""评测输入投影、缺失分数和物理HTTP费用保护；不联网、不加载RAGAS/模型。"""
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from tests.performance.ragas_dataset import tool_contexts, prepare
from tests.performance.ragas_judge import JudgeBudget, JudgeBudgetStop, JudgeTransport
from tests.performance.run_ragas import summarize

ROOT = Path(__file__).resolve().parents[2]


class RagasEvaluationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        (ROOT / "reports").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "reports")
        self.path = Path(self.temp.name)
        self.budgets = []

    def tearDown(self):
        for budget in self.budgets:
            if not budget.file.closed:
                budget.close()
        self.temp.cleanup()

    def budget(self, **kwargs):
        budget = JudgeBudget(self.path / "ledger.jsonl", **kwargs)
        self.budgets.append(budget)
        return budget

    def request_bytes(self, **kwargs):
        return json.dumps({"model": "deepseek-flash", "messages": [{"role": "user", "content": "fixture"}],
                           "max_tokens": 20, **kwargs}).encode()

    def test_contexts_are_actual_units_and_preserve_order_not_double_data(self):
        passage = {"title": "Fixture guide", "evidence_units": [{"text": "Original unit"}]}
        payload = {"ok": True, "passages": [passage, passage], "data": {"passages": [passage, passage]}}
        contexts, metadata = tool_contexts([{"name": "search_knowledge", "content": json.dumps(payload)}])
        self.assertEqual(contexts, ["Fixture guide\nOriginal unit"] * 2)
        self.assertEqual(len(metadata), 2)

    def test_missing_actual_units_fails_rather_than_substitute_document(self):
        payload = {"ok": True, "passages": [{"title": "guide", "content": "hindsight full document"}]}
        with self.assertRaises(ValueError):
            tool_contexts([{"name": "search_knowledge", "content": json.dumps(payload)}])

    def test_mean_excludes_missing_but_keeps_real_zero(self):
        samples = [{"sample_id": str(i), "profile": "sequential", "category": "knowledge"} for i in range(4)]
        rows = [{"sample_id": str(i), "metric": "faithfulness", "status": status, "score": score}
                for i, (status, score) in enumerate((("scored", 0), ("scored", 1), ("undefined", None), ("error", None)))]
        score = summarize(samples, rows)["single_turn_knowledge:sequential"]["faithfulness"]
        self.assertEqual(score["mean"], .5)
        self.assertEqual((score["valid"], score["planned"]), (2, 4))

    def test_ledger_restores_attempts_and_cost(self):
        budget = self.budget()
        call = budget.reserve(self.request_bytes())
        budget.finish(call, {"usage": {"prompt_tokens": 10, "completion_tokens": 20}, "model": "fixture"})
        total = budget.estimated_usd
        budget.close()
        again = self.budget()
        self.assertEqual(again.attempts, 1)
        self.assertEqual(again.estimated_usd, total)

    def test_unknown_usage_and_unfinished_calls_block_new_attempts(self):
        budget = self.budget()
        budget.reserve(self.request_bytes())
        budget.close()
        again = self.budget()
        with self.assertRaises(JudgeBudgetStop):
            again.reserve(self.request_bytes())

    def test_inflight_upper_cost_counts_towards_ceiling(self):
        budget = self.budget(max_usd=.0006)
        budget.reserve(self.request_bytes())
        with self.assertRaises(JudgeBudgetStop):
            budget.reserve(self.request_bytes())
        self.assertEqual(budget.attempts, 1)

    def test_unbounded_or_unpriced_calls_rejected_before_reservation(self):
        budget = self.budget()
        for options in ({"max_tokens": 2049}, {"max_tokens": None}, {"model": "other"}, {"stream": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                budget.reserve(self.request_bytes(**options))
        self.assertEqual(budget.attempts, 0)

    async def test_transport_counts_every_request_and_stops_on_unknown_usage(self):
        calls = []
        def respond(request):
            calls.append(request)
            return httpx.Response(200, json={"choices": []})
        budget = self.budget()
        transport = JudgeTransport(budget, self.path / "raw.jsonl", httpx.MockTransport(respond))
        async with httpx.AsyncClient(transport=transport) as client:
            await client.post("https://api.deepseek.com/v1/chat/completions", content=self.request_bytes())
            with self.assertRaises(JudgeBudgetStop):
                await client.post("https://api.deepseek.com/v1/chat/completions", content=self.request_bytes())
            with self.assertRaises(ValueError):
                await client.post("https://example.com/v1/chat/completions", content=self.request_bytes())
        self.assertEqual(len(calls), 1)
        self.assertEqual(budget.stopped, "unknown_provider_usage")

    def test_request_limit_survives_reopening_ledger(self):
        budget = self.budget(max_attempts=1)
        call = budget.reserve(self.request_bytes())
        budget.finish(call, {"usage": {"prompt_tokens": 1, "completion_tokens": 1}})
        budget.close()
        reopened = self.budget(max_attempts=1)
        with self.assertRaises(JudgeBudgetStop):
            reopened.reserve(self.request_bytes())

    def test_second_process_cannot_share_a_live_ledger(self):
        self.budget()
        with self.assertRaises(RuntimeError):
            JudgeBudget(self.path / "ledger.jsonl")

    def test_frozen_archive_projection_validates_both_profiles(self):
        rows = []
        for profile in ("sequential", "concurrent4"):
            rows.append({"id": "fixture-id", "profile": profile, "http_status": 200, "question": "fixture question",
                         "category": "knowledge", "tool_messages": [], "result": {"answer": "fixture answer"}})
        archive = self.path / "answers.jsonl"
        archive.write_text("\n".join(json.dumps(row) for row in rows))
        refs = self.path / "references.json"
        refs.write_text(json.dumps({"cases": {"fixture-id": {"reference": None, "basis": "fixture", "source_titles": []}}}))
        self.assertEqual(len(prepare(archive, refs)), 2)
        archive.write_text(json.dumps(rows[0]))
        with self.assertRaises(ValueError):
            prepare(archive, refs)
