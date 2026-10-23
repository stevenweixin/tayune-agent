"""知识库发布命令。默认只查看；stage/activate/rollback必须显式--write。"""
import argparse
import json
import os
from pathlib import Path

from app.rag.knowledge_releases import KnowledgeReleases
from scripts.build_knowledge_base import PROJECT_ROOT


def main():
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description="TAYUNE知识库版本管理")
    parser.add_argument("action", choices=["status", "stage", "activate", "rollback", "revoke"], nargs="?", default="status")
    parser.add_argument("--release")
    parser.add_argument("--embedding-revision", help="本地模型制品/版本标记，发布与服务须保持相同")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--document-id")
    parser.add_argument("--exclude-document-id", action="append", default=[], help="候选快照排除撤回的文档，可重复")
    args = parser.parse_args()
    releases = KnowledgeReleases(PROJECT_ROOT / "data/generated/knowledge_releases")
    if args.action == "status":
        print(json.dumps(releases.active(), indent=2))
        return
    if not args.write:
        parser.error("此操作会写入；确认目标为TAYUNE实例后加--write")
    if args.action == "revoke":
        if not args.document_id:
            parser.error("revoke需要--document-id")
        releases.revoke(args.document_id)
        print("文档已撤回，后续检索不再返回其片段。已完成的历史回答不会自动改写。")
        return
    from app.rag.vector_store import connect_client
    client = connect_client()
    try:
        if args.action == "stage":
            if not args.embedding_revision:
                parser.error("stage需要--embedding-revision")
            from app.rag.hybrid_embeddings import HybridBGEmbeddings
            from scripts.build_knowledge_base import read_chunks, INPUT_FILE, MODEL_PATH
            chunks = []
            for chunk in read_chunks(INPUT_FILE):
                if chunk.metadata.get("document_id") not in args.exclude_document_id:
                    chunks.append(chunk)
            model_path = Path(os.getenv("TAYUNE_BGE_MODEL_PATH", str(MODEL_PATH)))
            result = releases.stage(client, HybridBGEmbeddings(model_path), chunks, args.embedding_revision)
        elif args.action == "activate":
            if not args.release:
                parser.error("activate需要--release")
            result = releases.activate(client, args.release)
        else:
            result = releases.rollback(client)
        print(json.dumps({"id": result["id"], "collection": result["collection"]}))
        print("激活/回滚后重启后端再生效；现有进程继续固定使用启动时版本。")
    finally:
        client.close()


if __name__ == "__main__":
    main()
