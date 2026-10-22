"""复用正式混合召回和精排，向模型提供知识工具；不写Milvus。
是把能力包装成 LangChain Tool 给模型用。区别是商品工具查的是网站的实时数据,
这个工具查的是本地知识库(茶知识、冲泡指南、店铺政策)。"""
import asyncio
import logging
from time import perf_counter
from typing import Literal
from urllib.parse import urlparse
from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field
from app.rag.reranker import retrieve

logger = logging.getLogger("tayune_agent.knowledge")

class KnowledgeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1, max_length=500, description="Specific English tea or shop-policy question.")
    source_type: Literal["article", "brewing", "policy"] | None = Field(
        default=None, description="Optional knowledge category.")


def build_knowledge_tool(retriever, reranker, cpu):
    @tool(args_schema=KnowledgeInput)
    async def search_knowledge(query: str, source_type: str | None = None) -> dict:
        """Search approved TAYUNE tea knowledge, brewing guides and public policies.
        Returns evidence passages and source labels, not current prices or inventory.
        An optional source_type restricts retrieval; a brewing question may also
        need an article. Do not keep an unsuitable category just because it returned text.
        Scores are relevance scores, not accuracy probabilities. Weak or conflicting
        evidence needs clarification; do not claim retrieved text proves every assertion.
        """
        started = perf_counter()
        logger.info("stage=knowledge status=start source_type=%s", source_type or "all")

        try:
            documents, score = await cpu.run(retrieve, query, retriever, reranker, source_type, 6, 3)
        except asyncio.CancelledError:
            # Graph 的 wait_for 超时会取消等待；不吞掉取消，也不宣称后台线程已停止。
            logger.warning("stage=knowledge status=cancelled elapsed=%.3fs",
                           perf_counter() - started)
            raise
        except (ValueError,RuntimeError) as error:
            # 召回/精排的具体阶段在 retrieve 中记录；此处也可能是 CPUWorker 正忙。
            logger.warning("stage=knowledge status=failed error_type=%s elapsed=%.3fs",
                           type(error).__name__, perf_counter() - started)
            return {"ok": False, "error": "knowledge_unavailable"}
        passages = []
        for index, (document, relevance) in enumerate(documents, start=1):
            raw_url = document.metadata.get("source_url") or ""
            parsed = urlparse(raw_url)
            url = raw_url if parsed.scheme == "https" and parsed.netloc == "tayunetea.com" else None
            chunk_id = document.metadata.get("chunk_id")
            evidence_key = None
            if isinstance(chunk_id, str) and chunk_id.strip():
                # chunk_id来自检索结果，不由模型指定；URL为空也可以定位真实已审核片段。
                # 审核过滤仍由现有检索器负责，不在这里读取或公开本地文件路径。
                evidence_key = "chunk:" + chunk_id
            passages.append({"label": index, "title": document.metadata.get("title", "Knowledge"),
                             "text": document.page_content, "url": url,
                             "chunk_id": chunk_id, "evidence_key": evidence_key, "score": relevance})
        # 空结果不是执行异常；日志单独标记，模型仍需判断是否有足够证据。
        logger.info("stage=knowledge status=%s passages=%d elapsed=%.3fs",
                    "ok" if passages else "empty", len(passages), perf_counter() - started)
        return {"ok": True, "passages": passages, "top_score": score}

    return search_knowledge
