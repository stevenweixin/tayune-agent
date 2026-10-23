"""评测器本身也要能发现错误，不只检查正确固定样例。"""
import json
import unittest
from copy import deepcopy

from tests.evaluation.run_tasks import CASES, evaluate, replay


class BusinessReplayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]

    async def test_32_versioned_execution_contracts(self):
        self.assertEqual(len(self.cases), 32)
        self.assertEqual(len({case["id"] for case in self.cases}), 32)
        for case in self.cases:
            with self.subTest(case=case["id"]):
                result = await replay(case)
                self.assertTrue(result["contract_pass"], result)

    async def test_wrong_expected_argument_does_not_pass(self):
        case = deepcopy(next(case for case in self.cases if case["id"] == "tea-budget30"))
        case["expected"]["args"]["max_price_usd"] = "5"
        result = await replay(case)
        self.assertFalse(result["contract_pass"])
        self.assertFalse(result["checks"]["parameters"])

    def test_wrong_dependency_order_is_reported(self):
        case = next(case for case in self.cases if case["id"] == "product-detail")
        events = [{"kind": "tool_call", "tool": name} for name in ("get_product", "search_products")]
        result = evaluate(case, {"degraded": False, "sources": []}, events)
        self.assertFalse(result["checks"]["dependency_order"])


if __name__ == "__main__":
    unittest.main()
