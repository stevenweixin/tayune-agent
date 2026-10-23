"""
混合召回：从启动时选定的双向量集合检索，不写入。

输入问题，返回 [(LangChain Document, 排序分数), ...]。

用户问题
  ↓
HybridBGEmbeddings
  ↓
同时生成
dense 向量 + sparse 向量
  ↓
Milvus 两路检索
  ↓
WeightedRanker(0.7, 0.3)
  ↓
融合排序
  ↓
返回 Document + score
"""
from langchain_core.documents import Document
from pymilvus import AnnSearchRequest, WeightedRanker

from app.rag.hybrid_embeddings import HybridBGEmbeddings
from app.rag.hybrid_vector_store import COLLECTION

class HybridRetriever:
    def __init__(self,client,embeddings:HybridBGEmbeddings, collection_name=COLLECTION, knowledge_version="legacy", releases=None):
        self.client = client
        self.embeddings = embeddings
        self.collection = collection_name
        self.knowledge_version = knowledge_version
        self.releases = releases

    def search(self,query:str,top_k:int=10, source_type: str | None = None) -> list[tuple[Document,float]]:
        """两路各取 top_k 个候选,再融合选出最多 top_k 个不同记录."""
        if not isinstance(query,str) or not query.strip():
            raise ValueError("查询不能为空")
        if type(top_k) is not int or not 1 <= top_k <= 20:
            raise ValueError("top_k 必须为 1–20 的整数")
        expression = 'review_status == "approved"'
        if source_type is not None:
            if source_type not in ("article","brewing","policy"):
                raise ValueError("无效的知识类型")
            expression += f' and source_type == "{source_type}"'

        # 文档和问题使用同一个双向量模型; 不读取就稠密集合
        dense, sparse = self.embeddings.encode_query(query)
        if sparse:
            dense_request = AnnSearchRequest(
                data=[dense], anns_field="dense_vector",
                param = {"metric_type":"COSINE","params":{}},
                limit=top_k, expr=expression,
            )
            sparse_request = AnnSearchRequest(
                data=[sparse], anns_field="sparse_vector",
                param = {"metric_type":"IP","params":{"drop_ratio_search":0.0}},
                limit=top_k, expr=expression,
            )
            # 权重顺序对应reqs 顺序
            results = self.client.hybrid_search(
                collection_name=self.collection,
                reqs=[dense_request,sparse_request],
                ranker=WeightedRanker(0.7, 0.3),
                limit= top_k, output_fields=["text","metadata"],timeout=30,
            )
        else:
            # 问题没有非零词项时只做稠密检索,不能伪造token来凑稀疏请求
            results = self.client.search(
                collection_name=self.collection,data=[dense], anns_field="dense_vector",
                filter = expression,
                search_params = {"metric_type":"COSINE","params":{}},
                limit=top_k, output_fields= ["text","metadata"],timeout= 30,
            )

        documents = []
        if not results:
            return documents
        revoked = set(self.releases.revoked_documents()) if self.releases else set()
        for hit in results[0]:
            entity = hit["entity"]
            # 撤回立即作用于后续检索，不等整库重建；过滤后可能不足top_k，不伪称无相关知识。
            if entity["metadata"].get("document_id") in revoked:
                continue
            document = Document(page_content=entity["text"],metadata = entity["metadata"])
            document.metadata["knowledge_version"] = self.knowledge_version
            documents.append((document,float(hit["distance"])))
        # 混合分数与回退的余弦分数含义不同; 都只用于当前查询排序,不能当正确率
        return documents

def main() -> int:
    """只读验证入口"""
    import argparse

    from app.core.logger import setup_logging
    from app.rag.hybrid_vector_store import HybridTeaVectorStore
    from app.rag.vector_store import connect_client
    from scripts.build_knowledge_base import MODEL_PATH, PROJECT_ROOT

    parser = argparse.ArgumentParser(description="预览混合召回,不入库 不生成答案")
    parser.add_argument("--query", default="What is your return policy?")
    parser.add_argument("--source-type", choices=["article", "brewing", "policy"])
    parser.add_argument("--top-k", type=int, default=10)
    args = parser.parse_args()
    logger = setup_logging(PROJECT_ROOT / "logs/hybrid_retriever.log")
    client = None
    try:
        client = connect_client()
        embeddings = HybridBGEmbeddings(MODEL_PATH)
        # creat 默认为False: 若未完成双向量入口,报错而不是偷偷创建空集合
        HybridTeaVectorStore(client,embeddings).prepare_collection()
        retriever = HybridRetriever(client,embeddings)
        documents = retriever.search(args.query, args.top_k, args.source_type)
        for rank, (document, score) in enumerate(documents,start=1):
            source = document.metadata.get("source_url") or document.metadata.get("source_file")
            print(f" \n 第{rank} 条 | 排序分数:{score:.4f}")
            print(f"标题: {document.metadata.get('title')}\n来源: {source}")
            print(document.page_content)
        logger.info("混合召回预览完成: %d 个候选; 集合: %s", len(documents), COLLECTION)
        return 0
    except Exception:
        logger.exception("混合召回预览失败")
        return 1
    finally:
        if client is not None:
            client.close()

if __name__ == '__main__':
    raise SystemExit(main())
