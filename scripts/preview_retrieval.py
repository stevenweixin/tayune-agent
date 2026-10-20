"""只检索、不生成答案。运行：python -m scripts.preview_retrieval_1

问题 -> LangChain Embeddings -> Milvus -> Document + 余弦分数。
这还不是完整 RAG；下一组才根据证据生成回答并加入拒答规则。
"""

import argparse

from app.core.logger import setup_logging
from app.rag.embeddings import LocalBGEmbeddings
from app.rag.vector_store import TeaVectorStore,connect_client
from scripts.build_knowledge_base import MODEL_PATH,PROJECT_ROOT

def main() -> int:
    parser = argparse.ArgumentParser(description="预览稠密检索结果")
    parser.add_argument("--query", default="What is your return policy?")
    parser.add_argument("--source-type", choices=["article", "brewing", "policy"])
    args = parser.parse_args()
    logger = setup_logging(PROJECT_ROOT /  "logs/preview_retrieval.log")
    client = None
    try:
        client = connect_client()
        embeddings = LocalBGEmbeddings(MODEL_PATH)
        store = TeaVectorStore(client,embeddings)
        store.prepare_collection()
        results = store.similarity_search_with_score(args.query, k=3, source_type=args.source_type)
        if not results:
            logger.warning("未检索到结果,请检查集合数据或过滤条件")
        for rank, (document,score) in enumerate(results, start=1):
            # print 用于交互预览；不将全文写到持久化日志。
            source = document.metadata.get("source_url") or document.metadata.get("source_file")
            print(f"\n第 {rank} 条 | COSINE:{score:.4f}")
            print(f"标题: {document.metadata.get('title')}")
            print(f"来源: {source}")
            print(document.page_content)
        logger.info("检索完成:返回 %d 个片段，尚未生成答案", len(results))
        return 0
    except Exception:
        logger.exception("检索预览失败")
        return 1
    finally:
        if client is not None:
            client.close()

if __name__ == '__main__':
    raise SystemExit(main())