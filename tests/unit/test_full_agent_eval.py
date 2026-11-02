"""完整评测工具的口径回归；不发起模型或外部接口请求。"""
import unittest

from tests.performance.full_agent_cases import conversation_evidence, review_rubric, suite, evaluate_contract
from tests.performance.run_full_agent import summarize
from tests.performance.summarize_full_agent import review_counts


class FullAgentEvaluationTests(unittest.TestCase):
    def test_frozen_plan_has_sixty_three_unique_cases_and_three_multi_turn_groups(self):
        cases = suite()
        self.assertEqual(len(cases), 63)
        self.assertEqual(len({item["id"] for item in cases}), 63)
        self.assertEqual(len({item["session_group"] for item in cases if item["session_group"]}), 3)

    def test_rubric_is_business_specific(self):
        cases = suite()
        brewing = next(item for item in cases if item["question"] == "How should I brew oolong in a mug?")
        self.assertTrue(any("vessel" in text for text in review_rubric(brewing)))
        restricted = next(item for item in cases if item["category"] == "restricted")
        self.assertTrue(any("do not execute" in text for text in review_rubric(restricted)))

    def test_burst_degraded_http_200_is_not_a_pass(self):
        result = {"answer": "unavailable", "degraded": True, "sources": []}
        checked = evaluate_contract({"category": "burst"}, result, [], {}, 200)
        self.assertFalse(checked["contract_pass"])

    def test_burst_with_sources_and_no_degradation_passes_proxy(self):
        result = {"answer": "fixture", "degraded": False, "sources": [{"url": "https://tayunetea.com/guide/"}]}
        self.assertTrue(evaluate_contract({"category": "burst"}, result, [], {}, 200)["contract_pass"])
        self.assertFalse(evaluate_contract({"category": "burst"}, result, [], {}, 429)["contract_pass"])

    def test_export_excludes_system_prompts_and_keeps_actual_tool_arguments(self):
        checkpoint = {"messages": [{"type": "system", "data": {"content": "private prompt"}},
                                  {"type": "ai", "data": {"content": "", "tool_calls": [{"name": "search_products", "args": {"search": "tea"}}]}}]}
        result = conversation_evidence(checkpoint)
        self.assertEqual(len(result["conversation"]), 1)
        self.assertEqual(result["proposed_tool_calls"][0]["args"], {"search": "tea"})
        self.assertNotIn("private prompt", str(result))

    def test_rejection_latency_is_not_success_latency(self):
        rows = [{"category": "burst", "http_status": 429, "result": {}, "contract_pass": False,
                 "elapsed_ms": 1, "audit": []},
                {"category": "burst", "http_status": 200, "result": {"degraded": False}, "contract_pass": True,
                 "elapsed_ms": 2000, "audit": []}]
        result = summarize(rows)
        self.assertEqual(result["positive_completed_latency"]["p95_ms"], 2000)
        self.assertEqual(result["rejected_429"], 1)

    def test_review_requires_exactly_one_verdict_per_case(self):
        rows = [{"id": "a"}, {"id": "b"}]
        review = {"cases": [{"id": "a", "verdict": "partial"}, {"id": "a", "verdict": "partial"}]}
        with self.assertRaises(ValueError):
            review_counts(rows, review)

    def test_review_counts_preserve_clarification_and_reject_unknown_labels(self):
        rows = [{"id": "a"}]
        review = {"cases": [{"id": "a", "verdict": "appropriate_clarification"}]}
        self.assertEqual(review_counts(rows, review), {"appropriate_clarification": 1})
        review["cases"][0]["verdict"] = "correct"
        with self.assertRaises(ValueError):
            review_counts(rows, review)
