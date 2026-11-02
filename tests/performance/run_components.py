"""真实CPU模型和只读Milvus消融；不调用DeepSeek、不写数据库。

12条来源/原文锚点探针在检索前冻结。Hit只表示这些锚点命中，
不是穷举相关片段后的Recall，也不是独立人工评估的答案正确率。
"""
import argparse
import hashlib
import json
import os
import time
from pathlib import Path

import psutil
from dotenv import load_dotenv
from langchain_core.documents import Document

from app.core.logger import setup_logging
from app.core.runtime import ROOT, Runtime
from app.rag.hybrid_retriever import HybridRetriever, LANGUAGE_VALUES
from tests.performance.run_offline import distribution

PROBES = [
    ("Can I return opened tea?", "Shipping, returns, and contact.", "opened tea cannot be returned"),
    ("Where are international orders shipped from?", "Shipping, returns, and contact.", "prepared in China"),
    ("How long does international shipping take?", "Shipping, returns, and contact.", "2–4 weeks"),
    ("When are shipping charges shown?", "Shipping, returns, and contact.", "shown at checkout"),
    ("What should I do about a damaged tea parcel?", "Shipping, returns, and contact.", "damaged in transit"),
    ("Does the shop store my card security code?", "Privacy Policy.", "does not store your full card number"),
    ("How can I unsubscribe from marketing emails?", "Privacy Policy.", "unsubscribe from Tea Letters"),
    ("Which companies help the shop process orders and payments?", "Privacy Policy.", "PayPal for payment processing"),
    ("How much tea should I use to brew pu-erh in a mug?", "How to Brew Pu-erh Tea", "3-4 g"),
    ("What humidity is suitable for storing pu-erh cakes?", "How to Store Pu-erh Tea Cake: A Complete Guide to Proper Pu-erh Storage", "55% to 70%"),
    ("Is green tea heavily oxidized?", "Pure Freshness, Timeless Heritage: The Story of Chinese Green Tea", "minimal oxidation"),
    ("How much chenpi should a beginner use?", "Discover authentic 2020 Xinhui Meijiang aged Chenpi from Guangdong", "2–3 grams"),
]


class FrozenQueryEmbedding:
    def __init__(self, dense, sparse):
        self.dense, self.sparse = dense, sparse

    def encode_query(self, query):
        return self.dense, self.sparse


def hit(documents, title, anchor=None, top_k=3):
    for document, _ in documents[:top_k]:
        if document.metadata.get("title") == title:
            if anchor is None or anchor.casefold() in document.page_content.casefold():
                return True
    return False


def describe(documents):
    return [{"title": doc.metadata.get("title"), "chunk_id": doc.metadata.get("chunk_id"),
             "source_url": doc.metadata.get("source_url"), "text": doc.page_content, "score": score}
            for doc, score in documents]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--language", choices=["en", "zh"], help="Apply the SAME language scope to dense and hybrid")
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / "reports"):
        parser.error("Output must be below reports/")
    output.mkdir(parents=True, exist_ok=False)
    setup_logging(output / "backend.log")
    load_dotenv(ROOT / ".env", override=False)
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    documents = []
    for name in ("articles", "policies", "brewing"):
        documents.extend(json.loads(line) for line in (ROOT / f"data/generated/{name}.jsonl").read_text().splitlines())
    labels = []
    for query, title, anchor in PROBES:
        matching = [doc for doc in documents if doc["title"] == title and anchor.casefold() in doc["content"].casefold()]
        if not matching:
            raise ValueError("Gold anchor does not exist: " + title + " / " + anchor)
        labels.append({"query": query, "document_id": matching[0]["document_id"], "title": title, "anchor": anchor})
    frozen = json.dumps(labels, ensure_ascii=False, indent=2)
    (output / "frozen_labels.json").write_text(frozen, encoding="utf-8")
    runtime = Runtime(session_path=output / "unused.sqlite3")
    started = time.perf_counter()
    try:
        classifier, retriever, reranker = runtime.load_models()
        manifest = {"load_seconds": time.perf_counter() - started,
                    "rss_mb": psutil.Process().memory_info().rss / 1024 ** 2,
                    "torch_threads": __import__("torch").get_num_threads(),
                    "collection": retriever.collection, "knowledge_version": retriever.knowledge_version,
                    "gold_sha256": hashlib.sha256(frozen.encode()).hexdigest(),
                    "human_gold_review": "pending", "independent_gold": False,
                    "comparison": "same one-time query vectors, approved source set, top6 candidates; no source-type filter",
                    "language": args.language}
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2))
        rows = []
        expression = 'review_status == "approved"'
        if args.language:
            expression += ' and metadata["language"] in ' + json.dumps(LANGUAGE_VALUES[args.language], ensure_ascii=False)
        for index, label in enumerate(labels):
            query, title, anchor = label["query"], label["title"], label["anchor"]
            timings = {}
            started = time.perf_counter()
            classification = classifier.classify(query)
            timings["classifier_ms"] = (time.perf_counter() - started) * 1000
            started = time.perf_counter()
            dense, sparse = retriever.embeddings.encode_query(query)
            timings["embedding_ms"] = (time.perf_counter() - started) * 1000
            started = time.perf_counter()
            dense_raw = runtime.milvus.search(collection_name=retriever.collection, data=[dense],
                anns_field="dense_vector", filter=expression,
                search_params={"metric_type": "COSINE", "params": {}}, limit=6,
                output_fields=["text", "metadata"], timeout=30)
            dense_docs = [(Document(page_content=item["entity"]["text"], metadata=item["entity"]["metadata"]),
                           float(item["distance"])) for item in dense_raw[0]]
            revoked = set(retriever.releases.revoked_documents())
            dense_docs = [(doc, score) for doc, score in dense_docs if doc.metadata.get("document_id") not in revoked]
            timings["dense_database_ms"] = (time.perf_counter() - started) * 1000
            frozen_retriever = HybridRetriever(runtime.milvus, FrozenQueryEmbedding(dense, sparse),
                retriever.collection, retriever.knowledge_version, retriever.releases)
            started = time.perf_counter()
            hybrid = frozen_retriever.search(query, top_k=6, language=args.language)
            timings["hybrid_database_ms"] = (time.perf_counter() - started) * 1000
            started = time.perf_counter()
            ranked, _ = reranker.rerank_with_confidence(query, hybrid, top_k=3)
            timings["rerank_ms"] = (time.perf_counter() - started) * 1000
            modes = {"dense": dense_docs, "hybrid": hybrid, "hybrid_rerank": ranked}
            row = {**label, "index": index, "classification": classification, "timings": timings,
                   "results": {name: describe(items) for name, items in modes.items()},
                   "metrics": {name: {"document_hit_at_3": hit(items, title),
                                      "literal_anchor_hit_at_3": hit(items, title, anchor),
                                      "document_hit_at_6": hit(items, title, top_k=6)}
                               for name, items in modes.items()}}
            rows.append(row)
            with (output / "results.jsonl").open("a", encoding="utf-8") as file:
                file.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(json.dumps({"index": index, "timings": timings, "metrics": row["metrics"]}), flush=True)
        summary = {"n": len(rows), "language": args.language, "label_scope": "12 synthetic single-source literal-anchor probes; not Recall or Answer Correctness",
                   "latency_ms": {key: distribution([row["timings"][key] for row in rows]) for key in rows[0]["timings"]},
                   "metrics": {mode: {metric: sum(row["metrics"][mode][metric] for row in rows)
                                      for metric in rows[0]["metrics"][mode]} for mode in modes},
                   "rss_mb": psutil.Process().memory_info().rss / 1024 ** 2}
        (output / "summary.json").write_text(json.dumps(summary, indent=2))
    finally:
        if runtime.milvus is not None:
            runtime.milvus.close()


if __name__ == "__main__":
    main()
