"""助手维护的离线回归；不访问数据库、不加载权重、不调用付费 API。"""
import re
import unittest
from unittest.mock import patch

import numpy as np
from langchain_core.documents import Document

from app.rag import reranker as module
from app.rag.reranker import BGEReranker, retrieve


class FakeTokenizer:
    is_fast = True

    def encode(self, text, pair=None, add_special_tokens=True, truncation=False):
        count = len(re.findall(r"\S+", text))
        if pair is not None:
            count += len(re.findall(r"\S+", pair))
        if add_special_tokens:
            count += 4 if pair is not None else 2
        return [1] * count

    def num_special_tokens_to_add(self, pair=False):
        return 4 if pair else 2

    def __call__(self, text, **kwargs):
        offsets = []
        for match in re.finditer(r"\S+", text):
            offsets.append(match.span())
        return {"offset_mapping": offsets}


class FakeModel:
    def __init__(self):
        self.tokenizer = FakeTokenizer()
        self.seen_pairs = []
        self.scores = None

    def predict(self, pairs, **kwargs):
        self.seen_pairs = list(pairs)
        if self.scores is not None:
            return np.array(self.scores)
        scores = []
        for query, text in pairs:
            scores.append(0.9 if "TAIL_EVIDENCE" in text else 0.2)
        return np.array(scores)


class RerankerWindowTests(unittest.TestCase):
    def setUp(self):
        self.reranker = BGEReranker.__new__(BGEReranker)
        self.reranker.max_length = 512
        self.reranker.model = FakeModel()

    def document(self, words):
        return Document(page_content=" ".join(words), metadata={"chunk_id": "a", "source_url": "https://tayunetea.com/guide"})

    def test_short_and_exact_limit_preserve_original(self):
        document = self.document(["tea"] * 507)
        ranked, score = self.reranker.rerank_with_confidence("brew", [(document, 0.7)])
        self.assertIs(ranked[0][0], document)
        self.assertEqual(score, 0.2)
        self.assertEqual(len(self.reranker.model.seen_pairs), 1)

    def test_513_tokens_split_and_keep_tail(self):
        document = self.document(["tea"] * 507 + ["TAIL_EVIDENCE"])
        ranked, score = self.reranker.rerank_with_confidence("brew", [(document, 0.7)])
        result = ranked[0][0]
        self.assertEqual(score, 0.9)
        self.assertIn("TAIL_EVIDENCE", result.page_content)
        self.assertEqual(result.metadata["chunk_id"], document.metadata["chunk_id"])
        self.assertEqual(result.metadata["source_url"], document.metadata["source_url"])
        self.assertNotIn("rerank_window", document.metadata)
        span = result.metadata["rerank_window"]
        self.assertEqual(result.page_content, document.page_content[span["start"]:span["end"]])
        self.assertEqual(span["count"], 2)
        for query, text in self.reranker.model.seen_pairs:
            self.assertLessEqual(len(self.reranker.model.tokenizer.encode(query, text)), 512)

    def test_windows_cover_original_and_overlap(self):
        text = "  " + " ".join([f"词{i}" for i in range(900)]) + "  "
        windows = self.reranker._split_long_text("tea", text, 384)
        self.assertEqual(windows[0][1], 0)
        self.assertEqual(windows[-1][2], len(text))
        previous_end = 0
        for number, (window, start, end) in enumerate(windows):
            self.assertEqual(window, text[start:end])
            self.assertLessEqual(start, previous_end)
            self.assertGreater(end, previous_end)
            if number:
                self.assertLess(start, previous_end)
            previous_end = end

    def test_retokens_are_checked_before_scoring(self):
        class BoundaryTokenizer(FakeTokenizer):
            def encode(self, text, pair=None, **kwargs):
                tokens = super().encode(text, pair, **kwargs)
                return tokens + [1] * 5 if pair is not None else tokens
        self.reranker.model.tokenizer = BoundaryTokenizer()
        self.reranker.max_length = 100
        document = self.document(["tea"] * 180)
        self.reranker.rerank_with_confidence("brew", [(document, 0.7)])
        for query, text in self.reranker.model.seen_pairs:
            self.assertLessEqual(len(self.reranker.model.tokenizer.encode(query, text)), 100)

    def test_one_result_per_candidate_not_per_window(self):
        long_document = self.document(["tea"] * 600 + ["TAIL_EVIDENCE"])
        short_document = Document(page_content="short", metadata={"chunk_id": "b"})
        ranked, score = self.reranker.rerank_with_confidence("brew", [(short_document, .9), (long_document, .1)])
        self.assertEqual(len(ranked), 2)
        self.assertEqual([doc.metadata["chunk_id"] for doc, score in ranked], ["a", "b"])
        self.assertEqual(score, .9)

    def test_invalid_query_and_top_k(self):
        document = self.document(["tea"])
        for query, top_k in [(" ", 3), ("tea", 0), ("tea", True), ("tea " * 510, 3)]:
            with self.assertRaises(ValueError):
                self.reranker.rerank_with_confidence(query, [(document, .5)], top_k)
        self.assertFalse(self.reranker.model.seen_pairs)

    def test_empty(self):
        self.assertEqual(self.reranker.rerank_with_confidence("tea", []), ([], 0.0))
        self.assertFalse(self.reranker.model.seen_pairs)

    def test_per_document_and_total_budgets_do_not_silently_truncate(self):
        document = self.document(["tea"] * 900)
        with patch.object(module, "MAX_WINDOWS_PER_DOCUMENT", 1):
            with self.assertRaisesRegex(ValueError, "单个候选"):
                self.reranker.rerank_with_confidence("tea", [(document, .5)])
        with patch.object(module, "MAX_RERANK_PAIRS", 1):
            with self.assertRaisesRegex(ValueError, "总数"):
                self.reranker.rerank_with_confidence("tea", [(document, .5)])
        with self.assertRaisesRegex(ValueError, "候选数量"):
            self.reranker.rerank_with_confidence("tea", [(document, .5)] * 21)
        self.assertFalse(self.reranker.model.seen_pairs)

    def test_bad_model_outputs(self):
        document = self.document(["tea"] * 600)
        for scores in ([.2], [.2, float("nan")], [.2, 1.5]):
            self.reranker.model.scores = scores
            with self.assertRaises(ValueError):
                self.reranker.rerank_with_confidence("tea", [(document, .5)])

    def test_pipeline_preserves_filter(self):
        document = self.document(["tea"] * 600)
        class Retriever:
            def search(inner, query, top_k, source_type):
                self.assertEqual((query, top_k, source_type), ("brew", 6, "brewing"))
                return [(document, .5)]
        ranked, score = retrieve("brew", Retriever(), self.reranker, "brewing", 6, 3)
        self.assertEqual(len(ranked), 1)


if __name__ == "__main__":
    unittest.main()
