"""复用正式混合召回和精排，向模型提供知识工具；不写Milvus。
是把能力包装成 LangChain Tool 给模型用。区别是商品工具查的是网站的实时数据,
这个工具查的是本地知识库(茶知识、冲泡指南、店铺政策)。"""
from typing import Literal
from urllib.parse import urlparse
from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field
from app.rag.reranker import retrieve


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
        Scores are relevance scores, not accuracy probabilities. Weak or conflicting
        evidence needs clarification; do not claim retrieved text proves every assertion.
        """
        try:
            documents, score = await cpu.run(retrieve, query, retriever, reranker, source_type, 6, 3)
        except (ValueError, RuntimeError):
            return {"ok": False, "error": "knowledge_unavailable"}
        passages = []
        for index, (document, relevance) in enumerate(documents, start=1):
            raw_url = document.metadata.get("source_url", "")
            parsed = urlparse(raw_url)
            url = raw_url if parsed.scheme == "https" and parsed.netloc == "tayunetea.com" else None
            passages.append({"label": index, "title": document.metadata.get("title", "Knowledge"),
                             "text": document.page_content, "url": url,
                             "chunk_id": document.metadata.get("chunk_id"), "score": relevance})
        return {"ok": True, "passages": passages, "top_score": score}
    return search_knowledge
