"""Offline evidence-selection contracts; not a semantic accuracy benchmark."""
import json
import unittest

from app.agent.graph import register_source
from app.agent.grounding import check_grounded_answer, product_facts, record_units
from app.agent.answer_guard import FALLBACK


def selection(source="S1", evidence="E1", **extra):
    return json.dumps({"selections": [{"source_id": source, "evidence_id": evidence, **extra}]})


class GroundingTests(unittest.TestCase):
    def setUp(self):
        self.sources = []
        register_source(self.sources, "Jasmine Tea", "https://tayunetea.com/product/jasmine/")
        self.recipe = "For a mug: use 2-3 g per 200 ml, 80-85 C, for 2-3 minutes."
        record_units(self.sources, "S1", [self.recipe])

    def test_output_is_original_unit_with_trusted_inline_title(self):
        result = check_grounded_answer(selection(), self.sources)
        self.assertEqual(result["answer"], "Jasmine Tea:\n" + self.recipe)
        self.assertEqual(result["answer_parts"][0], {
            "text": "Jasmine Tea", "url": "https://tayunetea.com/product/jasmine/"})
        self.assertEqual(result["grounding_mode"], "extractive_units_v1")
        self.assertNotIn("teaspoon", result["answer"])
        self.assertNotIn("350", result["answer"])

    def test_valid_citation_cannot_authorize_new_teaspoon_or_scaled_recipe(self):
        for draft in ("Use a heaped teaspoon. [S1]", "Use 4-5 g per 350-400 ml. [S1]",
                      "[Jasmine](source:S1) is caffeine free."):
            with self.subTest(draft=draft), self.assertRaisesRegex(ValueError, "invalid_grounded_response"):
                check_grounded_answer(draft, self.sources)

    def test_model_cannot_supply_its_own_text_title_or_link(self):
        for key in ("text", "quote", "title", "url"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                check_grounded_answer(selection(**{key: "unsupported"}), self.sources)

    def test_prose_or_extra_fields_outside_selection_are_rejected(self):
        valid = selection()
        for draft in ("Here is the recipe: " + valid, valid + " Use a teaspoon.",
                      "```json\n" + valid + "\n```", valid[:-1] + ',"answer":"new claim"}'):
            with self.subTest(draft=draft), self.assertRaises(ValueError):
                check_grounded_answer(draft, self.sources)

    def test_unknown_source_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown_source_id"):
            check_grounded_answer(selection(source="S99"), self.sources)

    def test_unknown_or_previous_turn_unit_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown_evidence_id"):
            check_grounded_answer(selection(evidence="E9"), self.sources)
        with self.assertRaises(ValueError):
            check_grounded_answer(selection(), [])

    def test_repeated_selection_does_not_duplicate_text_or_links(self):
        item = json.loads(selection())["selections"][0]
        result = check_grounded_answer(json.dumps({"selections": [item, item]}), self.sources)
        self.assertEqual(result["answer"].count(self.recipe), 1)
        self.assertEqual(len(result["sources"]), 1)

    def test_internal_guide_has_no_invented_link(self):
        register_source(self.sources, "Internal guide", None, "chunk:private-id")
        record_units(self.sources, "S2", ["Opened tea cannot be returned because it is food."])
        result = check_grounded_answer(selection(source="S2"), self.sources)
        self.assertIn("cannot be returned because it is food", result["answer"])
        self.assertIn("internal guide", result["answer"])
        self.assertEqual(result["sources"], [])
        self.assertNotIn("private-id", result["answer"])

    def test_complete_line_is_preserved_including_exception(self):
        line = "Contact support for damage; refunds are case by case, not guaranteed."
        record_units(self.sources, "S1", [line])
        result = check_grounded_answer(selection(evidence="E2"), self.sources)
        self.assertIn(line, result["answer"])

    def test_same_url_chunks_and_detail_keep_stable_noncolliding_units(self):
        self.assertEqual(register_source(self.sources, "Other chunk", self.sources[0]["url"]), "S1")
        units = record_units(self.sources, "S1", [self.recipe, "New detail.", self.recipe])
        self.assertEqual([unit["evidence_id"] for unit in units], ["E1", "E2"])
        self.assertEqual(len(self.sources[0]["evidence_units"]), 2)

    def test_unknown_origin_has_no_selectable_units(self):
        self.assertEqual(record_units(self.sources, None, ["Not traceable."]), [])

    def test_unit_limit_skips_whole_lines_not_partial_claims(self):
        record_units(self.sources, "S1", ["\n".join("Line " + str(index) for index in range(100))])
        self.assertEqual(len(self.sources[0]["evidence_units"]), 80)
        self.assertEqual(self.sources[0]["evidence_units"][-1]["text"], "Line 78")

    def test_fixed_reply_still_works_without_evidence(self):
        self.assertEqual(check_grounded_answer("<reply:unavailable>", [])["answer"], FALLBACK)

    def test_empty_oversized_and_excessive_selections_fail_closed(self):
        item = json.loads(selection())["selections"][0]
        for draft in ("", "x" * 8001, '{"selections":[]}', json.dumps({"selections": [item] * 7})):
            with self.subTest(draft=draft[:40]), self.assertRaises(ValueError):
                check_grounded_answer(draft, self.sources)

    def test_render_limit_rejects_without_truncating_conditions(self):
        record_units(self.sources, "S1", ["x" * 12001])
        with self.assertRaisesRegex(ValueError, "grounded_answer_too_long"):
            check_grounded_answer(selection(evidence="E2"), self.sources)

    def test_missing_product_fields_remain_unknown(self):
        facts = product_facts({})
        self.assertIn("The listed price could not be confirmed.", facts)
        self.assertIn("Availability could not be confirmed.", facts)

    def test_product_options_preserve_range_currency_and_out_of_stock(self):
        facts = product_facts({"currency": "USD", "price_range": {"min": "20", "max": "30"},
                               "has_options": True, "is_in_stock": False})
        self.assertIn("Listed price range: USD 20 to USD 30.", facts)
        self.assertIn("Availability: out of stock.", facts)
        self.assertTrue(any("select a specification" in fact for fact in facts))


if __name__ == "__main__":
    unittest.main()
