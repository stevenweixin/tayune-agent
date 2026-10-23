"""不可变集合 + 本地激活指针；写入中途失败不会污染当前正在使用的集合。"""
import json
import os
import re
import secrets
from pathlib import Path

from app.core.execution import fingerprint

LEGACY = "tayune_bgem3_hybrid_v1"
PREFIX = "tayune_bgem3_release_"


def check_collection_name(name):
    if name != LEGACY and not re.fullmatch(PREFIX + r"[a-f0-9]{18}", name):
        raise ValueError("not_a_tayune_knowledge_collection")


class KnowledgeReleases:
    def __init__(self, directory: Path):
        self.directory = directory

    def atomic_json(self, path, value):
        self.directory.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix("." + secrets.token_hex(4) + ".tmp")
        try:
            with temp.open("x", encoding="utf-8") as file:
                json.dump(value, file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    def manifest(self, release_id):
        if not re.fullmatch(r"[a-f0-9]{18}", release_id):
            raise ValueError("invalid_release_id")
        manifest = json.loads((self.directory / (release_id + ".json")).read_text(encoding="utf-8"))
        if (manifest.get("id") != release_id or manifest.get("status") != "validated"
                or manifest.get("collection") != PREFIX + release_id):
            raise ValueError("release_not_validated")
        check_collection_name(manifest["collection"])
        return manifest

    def active(self):
        pointer = self.directory / "active.json"
        if not pointer.exists():
            return {"id": "legacy", "collection": LEGACY}
        release_id = json.loads(pointer.read_text(encoding="utf-8"))["id"]
        if release_id == "legacy":
            return {"id": "legacy", "collection": LEGACY}
        return self.manifest(release_id)

    def revoked_documents(self):
        path = self.directory / "revoked_documents.json"
        if not path.exists():
            return []
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError("invalid_revocation_registry")
        return value

    def revoke(self, document_id):
        if not isinstance(document_id, str) or not document_id or len(document_id) > 128:
            raise ValueError("invalid_document_id")
        revoked = self.revoked_documents()
        if document_id not in revoked:
            revoked.append(document_id)
        self.atomic_json(self.directory / "revoked_documents.json", sorted(revoked))

    def validate_contents(self, client, manifest):
        # 当前知识库规模小，用真实ID集合对比，不把upsert_count当成发布成功。
        rows = client.query(collection_name=manifest["collection"], filter="",
                            output_fields=["id", "text", "metadata", "review_status", "source_type"], limit=len(manifest["chunk_ids"]) + 1,
                            consistency_level="Strong", timeout=30)
        actual = {row["id"] for row in rows}
        expected = set(manifest["chunk_ids"])
        if len(rows) != len(expected) or actual != expected:
            raise ValueError("release_chunk_inventory_mismatch")
        for row in rows:
            digest = fingerprint({"content": row["text"], "metadata": row["metadata"]})
            if (digest != manifest["chunk_hashes"].get(row["id"]) or row["review_status"] != "approved"
                    or row["source_type"] != row["metadata"]["source_type"]):
                raise ValueError("release_content_mismatch")

    def stage(self, client, embeddings, chunks, embedding_revision):
        from app.rag.hybrid_vector_store import HybridTeaVectorStore
        from app.rag.vector_store import validate_chunks
        validate_chunks(chunks)
        if not chunks or len(chunks) > 10000 or not embedding_revision:
            raise ValueError("release_requires_1_to_10000_chunks_and_embedding_revision")
        inventory = []
        chunk_ids = []
        document_ids = set()
        chunk_hashes = {}
        for chunk in chunks:
            if chunk.metadata["review_status"] != "approved":
                raise ValueError("release_contains_unapproved_content")
            chunk_ids.append(chunk.metadata["chunk_id"])
            document_id = chunk.metadata.get("document_id")
            if not isinstance(document_id, str) or not document_id:
                raise ValueError("release_document_id_missing")
            if document_id in self.revoked_documents():
                raise ValueError("release_contains_revoked_document")
            document_ids.add(document_id)
            inventory.append({"content": chunk.page_content, "metadata": chunk.metadata})
            chunk_hashes[chunk.metadata["chunk_id"]] = fingerprint(inventory[-1])
        if len(set(chunk_ids)) != len(chunk_ids):
            raise ValueError("duplicate_chunk_id")
        content_hash = fingerprint({"documents": inventory, "embedding": embedding_revision})
        release_id = content_hash[:12] + secrets.token_hex(3)
        collection = PREFIX + release_id
        if client.has_collection(collection_name=collection):
            raise ValueError("immutable_release_already_exists")
        store = HybridTeaVectorStore(client, embeddings, collection)
        store.prepare_collection(create=True)
        store.add_documents(chunks)
        manifest = {"id": release_id, "collection": collection, "status": "validated",
                    "content_hash": content_hash, "embedding_revision": embedding_revision,
                    "chunk_ids": chunk_ids, "chunk_hashes": chunk_hashes,
                    "document_ids": sorted(document_ids), "count": len(chunks)}
        self.validate_contents(client, manifest)
        self.atomic_json(self.directory / (release_id + ".json"), manifest)
        return manifest

    def activate(self, client, release_id):
        current = self.active()
        manifest = self.manifest(release_id)
        if set(manifest["document_ids"]) & set(self.revoked_documents()):
            raise ValueError("release_contains_revoked_document")
        self.validate_contents(client, manifest)
        self.atomic_json(self.directory / "previous.json", {"id": current["id"]})
        self.atomic_json(self.directory / "active.json", {"id": release_id})
        return manifest

    def rollback(self, client):
        previous = json.loads((self.directory / "previous.json").read_text(encoding="utf-8"))
        if previous["id"] == "legacy":
            if self.revoked_documents():
                raise ValueError("cannot_prove_legacy_free_of_revoked_documents")
            if not client.has_collection(collection_name=LEGACY):
                raise ValueError("legacy_collection_missing")
            self.atomic_json(self.directory / "active.json", {"id": "legacy"})
            return {"id": "legacy", "collection": LEGACY}
        return self.activate(client, previous["id"])
