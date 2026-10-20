"""
精排
混合召回 10 个候选
→ 将“问题＋每段正文”交给 CrossEncoder
→ 重新打分、排序
→ 返回 Top 3 文档和第一名分数
"""
import math
from pathlib import Path

from langchain_core.documents import Document

class BGEReranker:
    def __init__(self,model_path:Path):
        if not model_path.is_dir():
            raise ValueError("本地reranker 目录不存在,不自动下载模型")
        from sentence_transformers import CrossEncoder
        from torch import nn

        self.max_length = 512
        self.model = CrossEncoder(
            str(model_path.resolve()),device="cpu",max_length=self.max_length,
            local_files_only=True, trust_remote_code=False,
            # 明确使用sigmoid 将分数压到 0-1 但这不是经过校准的正确概率
            default_activation_function=nn.Sigmoid(),
        )

    def rerank_with_confidence(
        self,query:str, documents:list[tuple[Document,float]],top_k: int = 3
    ) -> tuple[list[tuple[Document,float]],float]:
        """返回精排 Top K 和第一名的精排分数；"""
        if not isinstance(query,str) or not query.strip():
            raise ValueError("问题不能为空")
        if type(top_k) is not int or top_k <= 0:
            raise ValueError("top_k 必须是正整数")
        if not documents:
            return [], 0.0

        pairs = []
        for document, recall_score  in documents:
            # 混合召回分数用于选候选，不与精排模型分数相加。
            text = document.page_content
            token_ids = self.model.tokenizer.encode(
                query, text, add_special_tokens=True, truncation=False
            )
            # 项目规则：先明确报错，避免按字符数截断后仍悄悄丢掉关键证据。
            # 未来若遇到长片段，可设计窗口精排；本节点不改已有知识库。
            if len(token_ids) > self.max_length:
                raise ValueError("问题和片段合计超过 512 Token，需先处理长文本再精排")
            pairs.append((query,text))


        scores = self.model.predict(pairs,batch_size=4, show_progress_bar=False).tolist()
        if len(scores) != len(documents):
            raise ValueError("精排分数数量与候选数量不一致")
        ranked = []
        for index, score in enumerate(scores):
            score = float(score)
            if not math.isfinite(score) or not 0 <= score <= 1:
                raise  ValueError("精排模型返回了无效分数")
            document = documents[index][0]
            ranked.append((document,score))
        # item[1] 是分数；从高到低排序，不修改原候选列表或 Document。
        ranked.sort(key=lambda  item:item[1], reverse=True)
        top_results= ranked[:top_k]
        confidence = top_results[0][1]
        return top_results,confidence

def retrieve(query, retriever, reranker, source_type = None, recall_top_k= 10, rerank_top_k =3):
    """用普通函数串联召回和精排；模型由入口创建并复用。"""
    if type(rerank_top_k) is not int or rerank_top_k <= 0:
        raise ValueError("rerank_top_k 必须是正整数")
    candidates = retriever.search(query,top_k=recall_top_k, source_type=source_type)
    return reranker.rerank_with_confidence(query,candidates,top_k=rerank_top_k)


def main() -> int:
    import argparse

    from app.core.logger import setup_logging
    from app.rag.hybrid_embeddings import HybridBGEmbeddings
    from app.rag.hybrid_retriever import HybridRetriever
    from app.rag.hybrid_vector_store import HybridTeaVectorStore
    from app.rag.vector_store import connect_client
    from scripts.build_knowledge_base import MODEL_PATH,PROJECT_ROOT

    parser = argparse.ArgumentParser(description="混合召回 + 精排, 只读预览")
    parser.add_argument("--query", default="What is your return policy?")
    parser.add_argument("--source-type", choices=["article", "brewing", "policy"])
    args = parser.parse_args()
    logger =setup_logging(PROJECT_ROOT / "logs/reranker.log")
    client = None

    try:
        client = connect_client()
        embeddings = HybridBGEmbeddings(MODEL_PATH)
        HybridTeaVectorStore(client,embeddings).prepare_collection()
        retriever = HybridRetriever(client,embeddings)
        # 已核实存在的只读模型，不改权重、不重新下载。
        reranker = BGEReranker(MODEL_PATH.parent / "bge-reranker-large")
        documents, confidence = retrieve(args.query,retriever,reranker,args.source_type)
        for rank, (document, score) in enumerate(documents, start=1):
            source = document.metadata.get("source_url") or document.metadata.get("source_file")
            print(f"\n第 {rank} 条 | 精排分数 {score:.4f}")
            print(f"来源:{source}\n{document.page_content}")
        logger.info("精排完成: %d 条; Top-1 分数 %.4f (不是正确率)", len(documents),confidence)
        return 0
    except Exception:
        logger.exception("精排失败")
        return 1
    finally:
        if client is not None:
            client.close()

if __name__ == '__main__':
    raise SystemExit(main())