"""
精排
混合召回 10 个候选
→ 将“问题＋每段正文”交给 CrossEncoder
→ 重新打分、排序
→ 返回 Top 3 文档和第一名分数
"""
import logging
import math
from pathlib import Path
from time import perf_counter

from langchain_core.documents import Document
logger = logging.getLogger("tayune_agent.rag")

# 以下是低频 CPU 服务的项目预算，不是模型或 Milvus 的限制。
MAX_CANDIDATES = 20
MAX_WINDOW_TOKENS = 384
WINDOW_OVERLAP = 64
MAX_WINDOWS_PER_DOCUMENT = 8
MAX_RERANK_PAIRS = 40

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

    def _split_long_text(self, query: str, text: str, window_size: int) -> list[tuple[str, int, int]]:
        """返回 (窗口正文, 原文起始字符位置, 原文结束位置)，只用于超长候选。"""
        tokenizer = self.model.tokenizer
        if not tokenizer.is_fast:
            raise ValueError("长片段分窗需要支持字符位置的 fast tokenizer")
        # offset_mapping 是每个 Token 对应的原文字符范围。
        # 用它截取原文，而不是 decode 后重写正文，保证引用仍是原文中的连续内容。
        encoded = tokenizer(text, add_special_tokens=False, return_offsets_mapping=True)
        offsets = encoded["offset_mapping"]
        windows = []
        token_start = 0
        while token_start < len(offsets):
            if len(windows) >= MAX_WINDOWS_PER_DOCUMENT:
                logger.warning("stage=rerank_input error=window_budget_exhausted limit=%d",
                               MAX_WINDOWS_PER_DOCUMENT)
                raise ValueError("单个候选超过精排窗口预算，请先减小知识库片段")
            token_end = min(token_start + window_size, len(offsets))
            char_start = 0 if token_start == 0 else offsets[token_start][0]
            while token_end > token_start:
                char_end = len(text) if token_end == len(offsets) else offsets[token_end - 1][1]
                window_text = text[char_start:char_end]
                # 窗口起点变化可能改变分词，送入模型前再次核实真实的成对 Token 数。
                pair_tokens = tokenizer.encode(query, window_text, add_special_tokens=True, truncation=False)
                if len(pair_tokens) <= self.max_length:
                    break
                token_end -= 1
            if token_end <= token_start:
                raise ValueError("问题占用过多 Token，无法容纳精排窗口")
            windows.append((window_text, char_start, char_end))
            if token_end == len(offsets):
                break
            # 重叠减少跨边界上下文的丢失；很短的窗口缩小重叠，保证下一次一定前进。
            overlap = min(WINDOW_OVERLAP, (token_end - token_start) // 4)
            token_start = token_end - overlap
        return windows

    def rerank_with_confidence(
        self, query: str, documents: list[tuple[Document, float]], top_k: int = 3
    ) -> tuple[list[tuple[Document, float]], float]:
        """每个候选取最高窗口分，返回 Top K 证据及第一名分数；不是正确概率。"""
        if not isinstance(query, str) or not query.strip():
            raise ValueError("问题不能为空")
        if type(top_k) is not int or top_k <= 0:
            raise ValueError("top_k 必须是正整数")
        if not documents:
            return [], 0.0
        if len(documents) > MAX_CANDIDATES:
            raise ValueError("候选数量超过精排预算")

        tokenizer = self.model.tokenizer
        query_tokens = tokenizer.encode(query, add_special_tokens=False, truncation=False)
        # 512 是问题、正文及成对特殊 Token 的总和，不能全部分配给正文。
        available_tokens = self.max_length - len(query_tokens) - tokenizer.num_special_tokens_to_add(pair=True)
        if available_tokens <= 0:
            logger.warning("stage=rerank_input error=query_too_long")
            raise ValueError("问题过长，无法为精排正文预留 Token")
        window_size = min(MAX_WINDOW_TOKENS, available_tokens)

        pairs = []
        pair_sources = []
        window_counts = []
        for candidate_number, (document, recall_score) in enumerate(documents,start=1):
            # 混合召回分数用于选候选，不与精排模型分数相加。
            text = document.page_content
            token_ids = self.model.tokenizer.encode(
                query,text,add_special_tokens= True, truncation=False
            )
            # 只记候选序号和长度，不把用户问题或文档正文写入日志。
            logger.info("stage=rerank_input candidate=%d tokens=%d limit=%d",
                        candidate_number,len(token_ids), self.max_length)
            if len(token_ids) > self.max_length:
                windows = self._split_long_text(query, text, window_size)
            else:
                windows = [(text, 0, len(text))]
            # 不只处理前几个窗口后假装全文完成；超过预算明确失败并交给工具层降级。
            if len(pairs) + len(windows) > MAX_RERANK_PAIRS:
                logger.warning("stage=rerank_input error=pair_budget_exhausted limit=%d", MAX_RERANK_PAIRS)
                raise ValueError("本次精排窗口总数超过预算")
            window_counts.append(len(windows))
            logger.info("stage=rerank_windows candidate=%d windows=%d", candidate_number, len(windows))
            for window_text, char_start, char_end in windows:
                pairs.append((query, window_text))
                pair_sources.append((candidate_number - 1, char_start, char_end))

        scores = self.model.predict(pairs, batch_size=4, show_progress_bar=False).tolist()
        if len(scores) != len(pairs):
            raise ValueError("精排分数数量与窗口数量不一致")
        best_results = [None] * len(documents)
        for index, score in enumerate(scores):
            score = float(score)
            if not math.isfinite(score) or not 0 <= score <= 1:
                raise ValueError("精排模型返回了无效分数")
            candidate_index, char_start, char_end = pair_sources[index]
            previous = best_results[candidate_index]
            if previous is not None and score <= previous[1]:
                continue
            document = documents[candidate_index][0]
            if window_counts[candidate_index] > 1:
                metadata = dict(document.metadata)
                metadata["rerank_window"] = {"start": char_start, "end": char_end,
                                             "original_chars": len(document.page_content),
                                             "count": window_counts[candidate_index]}
                # 给 LLM 的证据与实际评分正文一致；原 Document 和数据库均不修改。
                document = Document(page_content=document.page_content[char_start:char_end], metadata=metadata)
            best_results[candidate_index] = (document, score)
        ranked = []
        for result in best_results:
            ranked.append(result)
        # item[1] 是分数；从高到低排序，不修改原候选列表或 Document。
        ranked.sort(key=lambda item: item[1], reverse=True)
        top_results = ranked[:top_k]
        confidence = top_results[0][1]
        return top_results, confidence

def retrieve(query,retriever,reranker,source_type=None,recall_top_k=10,rerank_top_k=3):
    """用普通函数串联召回和精排；模型由入口创建并复用。"""
    if type(rerank_top_k) is not int or rerank_top_k <=0:
        raise ValueError("rerank_top_k 必须是正整数")
    # perf_counter 只用来测耗时，不受电脑校时影响；单位是秒。
    started = perf_counter()
    logger.info("stage=recall status=start top_k=%d",recall_top_k)
    try:
        candidates = retriever.search(query,top_k=recall_top_k,source_type=source_type)
    except Exception as error:
        # 这里只记异常类型；裸 raise 把原异常继续交给知识工具处理。
        # 不用 logger.exception，避免第三方异常堆栈携带 URL、请求内容等信息。
        logger.warning("stage=recall status=failed error_type=%s elapsed=%.3fs",
                       type(error).__name__,perf_counter()-started)
        raise
    logger.info("stage=recall status=ok candidates=%d elapsed=%.3fs",
                len(candidates), perf_counter() - started)
    started = perf_counter()
    logger.info("stage=rerank status=start candidates=%d", len(candidates))
    try:
        documents, score = reranker.rerank_with_confidence(query, candidates, top_k=rerank_top_k)
    except Exception as error:
        logger.warning("stage=rerank status=failed error_type=%s elapsed=%.3fs",
                       type(error).__name__,perf_counter()-started)
        raise
    logger.info("stage=rerank status=ok returned=%d elapsed=%.3fs",
                len(documents), perf_counter() - started)
    return documents, score


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
