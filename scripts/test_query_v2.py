"""第二版数据隔离检查；不训练模型。"""
import copy
import hashlib
import json
import unittest

from scripts.prepare_query_v2 import OUTPUT_DIR, OLD_DIR, read_jsonl, validate_splits


class QueryV2Tests(unittest.TestCase):
    def setUp(self):
        self.splits = {}
        for name in ("train", "validation", "test"):
            self.splits[name] = read_jsonl(OUTPUT_DIR / f"{name}.jsonl")

    def test_counts_and_integrity(self):
        validate_splits(self.splits)
        self.assertEqual([len(rows) for rows in self.splits.values()], [252, 54, 120])
        with (OUTPUT_DIR / "manifest.json").open(encoding="utf-8") as file:
            manifest = json.load(file)
        for name in self.splits:
            digest = hashlib.sha256((OUTPUT_DIR / f"{name}.jsonl").read_bytes()).hexdigest()
            self.assertEqual(digest, manifest["files"][name]["sha256"])

    def test_old_test_not_in_training(self):
        old = {row["id"] for row in read_jsonl(OLD_DIR / "test.jsonl")}
        train = {row["id"] for row in self.splits["train"]}
        self.assertFalse(old & train)

    def test_generated_not_marked_human_approved(self):
        for rows in self.splits.values():
            for row in rows:
                if row["id"].startswith("V2-"):
                    self.assertEqual(row["review_status"], "assistant_reviewed_experiment")
                    self.assertEqual(row["reviewed_by"], "assistant")

    def test_contrast_group_can_have_multiple_labels(self):
        grouped = {}
        for row in self.splits["train"]:
            grouped.setdefault(row["group_id"], set()).add(row["label"])
        self.assertTrue(any(len(labels) > 1 for labels in grouped.values()))
        validate_splits(self.splits)

    def test_reject_cross_split_group(self):
        bad = copy.deepcopy(self.splits)
        bad["test"][0]["group_id"] = bad["train"][0]["group_id"]
        with self.assertRaises(ValueError):
            validate_splits(bad)

    def test_reject_duplicate_text(self):
        bad = copy.deepcopy(self.splits)
        bad["test"][0]["text"] = bad["train"][0]["text"]
        with self.assertRaises(ValueError):
            validate_splits(bad)


if __name__ == "__main__":
    unittest.main()
