"""版本切换使用内存Milvus替身，绝不连接19532或课堂服务。"""
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

from langchain_core.documents import Document

from app.rag.knowledge_releases import KnowledgeReleases, LEGACY


class FakeEmbeddings:
    def encode(self, texts):
        return [[0.0] * 1024 for _ in texts], [{1: 1.0} for _ in texts]


class FakeSchema:
    def add_field(self, **kwargs):
        pass


class FakeIndexes:
    def add_index(self, **kwargs):
        pass


class FakeMilvus:
    def __init__(self):
        self.collections = {LEGACY: []}

    def has_collection(self, collection_name):
        return collection_name in self.collections

    def create_schema(self, **kwargs):
        return FakeSchema()

    def prepare_index_params(self):
        return FakeIndexes()

    def create_collection(self, collection_name, **kwargs):
        self.collections[collection_name] = []

    def load_collection(self, **kwargs):
        pass

    def upsert(self, collection_name, data, **kwargs):
        self.collections[collection_name].extend(data)
        return {"upsert_count": len(data)}

    def query(self, collection_name, **kwargs):
        return self.collections[collection_name]


def chunk(text, chunk_id="chunk-a", doc_id="doc-a"):
    return Document(page_content=text, metadata={"chunk_id": chunk_id, "document_id": doc_id,
                    "review_status": "approved", "source_type": "brewing", "source_file": "approved-guide.md"})


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.releases = KnowledgeReleases(Path(self.temp.name))
        self.client = FakeMilvus()

    def tearDown(self):
        self.temp.cleanup()

    def stage(self, text="v1", chunk_id="chunk-a", doc_id="doc-a"):
        return self.releases.stage(self.client, FakeEmbeddings(), [chunk(text, chunk_id, doc_id)], "fixture-bge-m3")

    def test_stage_does_not_change_active_or_legacy(self):
        manifest = self.stage()
        self.assertEqual(self.releases.active()["id"], "legacy")
        self.assertEqual(self.client.collections[LEGACY], [])
        self.assertEqual(manifest["count"], 1)

    def test_activation_and_rollback_select_exact_inventory(self):
        first = self.stage()
        self.releases.activate(self.client, first["id"])
        second = self.stage("edited v2", "chunk-b")
        self.releases.activate(self.client, second["id"])
        self.assertEqual(self.releases.active()["chunk_ids"], ["chunk-b"])
        self.releases.rollback(self.client)
        self.assertEqual(self.releases.active()["id"], first["id"])

    def test_failed_inventory_validation_cannot_activate(self):
        manifest = self.stage()
        self.client.collections[manifest["collection"]].append({"id": "unexpected"})
        with self.assertRaises(ValueError):
            self.releases.activate(self.client, manifest["id"])
        self.assertEqual(self.releases.active()["id"], "legacy")

    def test_revoked_content_cannot_be_republished_or_rolled_back(self):
        manifest = self.stage()
        self.releases.revoke("doc-a")
        with self.assertRaises(ValueError):
            self.releases.activate(self.client, manifest["id"])
        with self.assertRaises(ValueError):
            self.stage()
        self.assertEqual(self.releases.revoked_documents(), ["doc-a"])

    def test_matching_ids_but_modified_text_cannot_be_activated(self):
        manifest = self.stage()
        self.client.collections[manifest["collection"]][0]["text"] = "unexpected replacement"
        with self.assertRaisesRegex(ValueError, "content_mismatch"):
            self.releases.activate(self.client, manifest["id"])
        self.assertEqual(self.releases.active()["id"], "legacy")

    def test_manifest_paths_cannot_escape_directory(self):
        for value in ("../other", "legacy", "b" * 200):
            with self.assertRaises(ValueError):
                self.releases.manifest(value)

    def test_revocation_filters_subsequent_retrieval_without_restarting(self):
        from app.rag.hybrid_retriever import HybridRetriever
        rows = [[{"entity": {"text": "withdrawn", "metadata": {"document_id": "doc-a"}}, "distance": 1.0},
                 {"entity": {"text": "current", "metadata": {"document_id": "doc-b"}}, "distance": 0.9}]]
        client = SimpleNamespace(search=lambda **kwargs: rows)
        embeddings = SimpleNamespace(encode_query=lambda query: ([0.0] * 1024, {}))
        retriever = HybridRetriever(client, embeddings, releases=self.releases)
        self.assertEqual(len(retriever.search("fixture")), 2)
        self.releases.revoke("doc-a")
        result = retriever.search("fixture")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0].page_content, "current")


if __name__ == "__main__":
    unittest.main()
