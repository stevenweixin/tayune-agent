"""服务启动一次加载资源，退出关闭；不会入库或下载模型。
重模型加载失败会阻止启动，不能带着缺失知识库对外声称服务就绪。

每个模块都是一个零件。这个模块的作用是:服务启动时,按顺序把所有零件创建好、接线、装配成可用的
Agent;服务退出时,按顺序关掉。
"""

import os
import math
import asyncio
from importlib.metadata import version
from pathlib import Path
import httpx
from dotenv import load_dotenv
from app.integrations.deepseek import read_config
from app.integrations.deepseek_tools import ToolModel
from app.integrations.woocommerce import WooCommerceClient
from app.agent.product_tool import build_product_tools
from app.agent.knowledge_tool import build_knowledge_tool
from app.agent.graph import build_graph
from app.agent.intent_review import IntentReviewer
from app.core.cpu_worker import CPUWorker
from app.database.session import SessionStore
from app.database.runs import RunStore
from app.integrations.model_gateway import ModelGateway
from app.core.execution import fingerprint

ROOT = Path(__file__).resolve().parents[2]


class Runtime:
    def __init__(self):
        self.http = None
        self.sync_http = None
        self.milvus = None
        self.sessions = None
        self.cpu = CPUWorker()
        self.classifier = None
        self.graph = None
        self.intent_reviewer = None
        self.runs = None
        self.gateway = None
        self.versions = {}
        self.cleanup_task = None
    async def start(self):
        # 只在服务入口加载，环境变量优先，兼容已完成Milvus配置读取函数。
        load_dotenv(ROOT / ".env", override=False) # override=False:如果环境变量已存在,不覆盖。部署时服务器设置的环境变量优先于本地文件。
        config = read_config()
        self.http = httpx.AsyncClient(trust_env=False)
        self.sync_http = httpx.Client(trust_env=False)
        self.sessions = SessionStore(ROOT / "data/generated/tayune_sessions.sqlite3")
        self.runs = RunStore(self.sessions)
        self.sessions.runs = self.runs
        self.sessions.cleanup()
        self.cleanup_task = asyncio.create_task(self.cleanup_loop())
        self.classifier, retriever, reranker = await self.cpu.run(self.load_models)
        tools = build_product_tools(WooCommerceClient(self.http))
        tools.append(build_knowledge_tool(retriever,reranker,self.cpu))
        from langchain_openai import ChatOpenAI
        # SDK重试参数必须在创建时设置；复制已创建对象不一定改变内部HTTP客户端。
        llm = ChatOpenAI(model=config["DEEPSEEK_MODEL"], api_key=config["DEEPSEEK_API_KEY"],
            base_url=config["DEEPSEEK_BASE_URL"], max_retries=0, timeout=25,
            max_tokens=512, use_responses_api=False, http_client=self.sync_http,
            http_async_client=self.http, extra_body={"thinking": {"type": "disabled"}})
        routes = {"main": llm, "review": llm, "summary": llm}
        names = {"main": config["DEEPSEEK_MODEL"], "review": config["DEEPSEEK_MODEL"], "summary": config["DEEPSEEK_MODEL"]}
        # 默认三个角色复用现有模型；仅显式配置角色模型时才改变路由，不增加供应商。
        for role in routes:
            name = (os.getenv("TAYUNE_" + role.upper() + "_MODEL", "").strip() or names[role])
            if name != names[role]:
                routes[role] = ChatOpenAI(model=name, api_key=config["DEEPSEEK_API_KEY"],
                    base_url=config["DEEPSEEK_BASE_URL"], max_retries=0, timeout=25,
                    max_tokens=512, use_responses_api=False, http_client=self.sync_http,
                    http_async_client=self.http, extra_body={"thinking": {"type": "disabled"}})
                names[role] = name
        fallback = None
        fallback_name = os.getenv("TAYUNE_FALLBACK_MODEL", "").strip()
        if fallback_name:
            fallback = (ChatOpenAI(model=fallback_name, api_key=config["DEEPSEEK_API_KEY"],
                base_url=config["DEEPSEEK_BASE_URL"], max_retries=0, timeout=25,
                max_tokens=512, use_responses_api=False, http_client=self.sync_http,
                http_async_client=self.http, extra_body={"thinking": {"type": "disabled"}}), fallback_name)
        rates = []
        for key in ("TAYUNE_INPUT_USD_PER_MILLION", "TAYUNE_OUTPUT_USD_PER_MILLION"):
            raw = os.getenv(key, "").strip()
            rate = float(raw) if raw else None
            if rate is not None and (not math.isfinite(rate) or rate < 0):
                raise ValueError("invalid_model_cost_rate")
            rates.append(rate)
        self.gateway = ModelGateway(routes, names, fallback, rates[0], rates[1])
        self.intent_reviewer = IntentReviewer(self.gateway.role("review"))
        self.graph = build_graph(ToolModel(self.gateway.role("main"),tools),tools)
        source_versions = {}
        for source in (ROOT / "app").rglob("*.py"):
            if source.stem.endswith("_1"):
                continue
            source_versions[str(source.relative_to(ROOT))] = fingerprint(source.read_text(encoding="utf-8"))
        self.versions = {"code": fingerprint(source_versions), "models": names,
                         "fallback_model": fallback_name or None,
                         "knowledge": getattr(self, "knowledge_version", "legacy"), "tools": "public-read-v1",
                         "dependencies": {name: version(name) for name in ("langchain-core", "langchain-openai", "langgraph", "pydantic")}}

    def load_models(self):
        from app.rag.vector_store import connect_client
        from app.rag.hybrid_embeddings import HybridBGEmbeddings
        from app.rag.hybrid_vector_store import HybridTeaVectorStore
        from app.rag.hybrid_retriever import HybridRetriever
        from app.rag.reranker import BGEReranker
        from app.rag.query_classifier import QueryClassifier
        from scripts.build_knowledge_base import MODEL_PATH
        model_path = Path(os.getenv("TAYUNE_BGE_MODEL_PATH", str(MODEL_PATH)))
        rerank_path = Path(os.getenv("TAYUNE_RERANK_MODEL_PATH", str(model_path.parent / "bge-reranker-large")))
        self.milvus = connect_client()
        embeddings = HybridBGEmbeddings(model_path)
        from app.rag.knowledge_releases import KnowledgeReleases
        self.releases = KnowledgeReleases(ROOT / "data/generated/knowledge_releases")
        active = self.releases.active()
        self.knowledge_collection = active["collection"]
        self.knowledge_version = active["id"]
        if active["id"] != "legacy" and os.getenv("TAYUNE_EMBEDDING_REVISION") != active["embedding_revision"]:
            raise ValueError("knowledge_embedding_revision_mismatch")
        HybridTeaVectorStore(self.milvus, embeddings, active["collection"]).prepare_collection(create=False)
        return QueryClassifier(), HybridRetriever(self.milvus, embeddings, active["collection"], active["id"], self.releases), BGEReranker(rerank_path)

    def snapshot_versions(self):
        return {**self.versions, "revocations": fingerprint(self.releases.revoked_documents())}

    async def cleanup_loop(self):
        while True:
            await asyncio.sleep(60)
            self.sessions.cleanup()
    async def close(self):  # 后创建的、被依赖的,先关;和启动顺序大致相反。
        if self.cleanup_task is not None:
            self.cleanup_task.cancel()
            try:
                await self.cleanup_task
            except asyncio.CancelledError:
                pass
        await self.cpu.close()
        if self.milvus is not None:
            self.milvus.close()
        if self.http is not None:
            await self.http.aclose()
        if self.sync_http is not None:
            self.sync_http.close()
        if self.sessions is not None:
            self.sessions.close()
