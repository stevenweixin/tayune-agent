"""评测工具自身的预算和口径不能失真；不调用任何外部服务。"""
import tempfile
import time
import unittest
from pathlib import Path

from langchain_core.messages import AIMessage

from app.core.runtime import ROOT, Runtime
from tests.performance.budget import EvaluationBudget
from tests.performance.cases import evaluation_cases
from tests.performance.run_offline import distribution


class EngineeringEvaluationTests(unittest.TestCase):
    def test_runtime_keeps_production_default(self):
        self.assertEqual(Runtime().session_path, ROOT / "data/generated/tayune_sessions.sqlite3")

    def test_runtime_accepts_isolated_path(self):
        self.assertEqual(Runtime(Path("isolated.sqlite3")).session_path, Path("isolated.sqlite3"))

    def test_percentiles_and_empty(self):
        self.assertIsNone(distribution([])["p95_ms"])
        self.assertEqual(distribution([5, 1, 2, 3, 4])["p95_ms"], 5)

    def test_dataset_is_fixed_sixty(self):
        cases = evaluation_cases()
        self.assertEqual(len(cases), 60)
        self.assertEqual(len({case["id"] for case in cases}), 60)

    def test_physical_attempt_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            budget = EvaluationBudget(Path(directory) / "usage.jsonl", max_attempts=1)
            budget.reserve()
            with self.assertRaises(RuntimeError):
                budget.reserve()

    def test_unknown_usage_stops_next_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            budget = EvaluationBudget(Path(directory) / "usage.jsonl")
            number = budget.reserve()
            budget.finish(number, "main", time.perf_counter(), error=TimeoutError())
            self.assertEqual(budget.stopped, "unknown_provider_usage")
            with self.assertRaises(RuntimeError):
                budget.reserve()

    def test_conservative_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            budget = EvaluationBudget(Path(directory) / "usage.jsonl")
            response = AIMessage(content="ok", usage_metadata={"input_tokens": 1000,
                                "output_tokens": 100, "total_tokens": 1100})
            budget.finish(budget.reserve(), "main", time.perf_counter(), response=response)
            self.assertAlmostEqual(budget.estimated_usd, .00042)


if __name__ == "__main__":
    unittest.main()
