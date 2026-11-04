"""规模/质量评测的口径检查，不请求模型或外部服务。"""
import unittest

from tests.performance.quality_cases import EXPECTED, case_groups, quality_cases
from tests.performance.run_scale import summarize_rows
from tests.performance.analyze_scale_quality import quality_summary


class ScaleQualityTests(unittest.TestCase):
    def test_frozen_quality_has_twenty_four_unique_cases_and_twenty_one_groups(self):
        cases = quality_cases()
        self.assertEqual(len(cases), 24)
        self.assertEqual({row["id"] for row in cases}, set(EXPECTED))
        self.assertEqual(len(case_groups(cases)), 21)

    def test_three_multi_turn_groups_keep_first_question_before_followup(self):
        groups = [group for group in case_groups(quality_cases()) if len(group) > 1]
        self.assertEqual(len(groups), 3)
        self.assertEqual([row["id"] for row in groups[1]], ["multi-jasmine-57", "multi-jasmine-58"])

    def test_missing_evidence_and_no_match_are_business_rules_not_only_urls(self):
        self.assertIn("no match", EXPECTED["product-01"])
        self.assertIn("another tea", EXPECTED["knowledge-31"])

    def test_fast_rejections_do_not_improve_completed_latency(self):
        rows = [{"status": 429, "business_success": False, "elapsed_ms": 1, "started_offset_seconds": 0},
                {"status": 200, "business_success": True, "elapsed_ms": 1000, "started_offset_seconds": .1}]
        result = summarize_rows(rows)
        self.assertEqual(result["completed_latency"]["p95_ms"], 1000)
        self.assertEqual(result["http_429"], 1)

    def test_transport_failure_is_not_an_http_5xx(self):
        result = summarize_rows([{"status": 0, "business_success": False, "elapsed_ms": 100,
                                  "started_offset_seconds": 0}])
        self.assertEqual(result["transport_failures"], 1)
        self.assertEqual(result["http_5xx"], 0)
        self.assertEqual(result["completed_latency"]["n"], 0)

    def test_effective_quality_keeps_rejected_submissions_in_denominator(self):
        rows = [{"id": "a", "profile": "sequential", "category": "product", "http_status": 429,
                 "result": {}, "within_deadline": True}]
        reviews = [{"id": "a", "profile": "sequential", "verdict": "meets"}]
        result = quality_summary(rows, reviews)["sequential"]
        self.assertEqual(result["positive_effective_within15s"], 0)
        self.assertEqual(result["positive_effective_denominator"], 1)

    def test_quality_review_cannot_silently_skip_a_profile(self):
        rows = [{"id": "a", "profile": "sequential"}, {"id": "a", "profile": "concurrent4"}]
        with self.assertRaises(ValueError):
            quality_summary(rows, [{"id": "a", "profile": "sequential", "verdict": "meets"}])
