"""Milvus / Zilliz 存储封装。

TeaVectorStore 是项目自己的类，不是 langchain-milvus 的内置类。
只做 BGE-M3 稠密检索；不删集合、不实现稀疏检索或 Rerank。
"""
import math
import os
from urllib.parse import urlparse

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

COLLECTION = "tayune_bgem3_dense_v1"
DIMENSION = 1024
COLLECTION_DESCRIPTION = "TAYUNE reference: local BGE-M3 dense normalized 1024 COSINE v1"

def get_connection_options() -> dict:
    """
    带 5 层严格安检 的配置读取函数
    只读 TAYUNE 专属配置；缺少配置时拒绝连接，避免误用课堂服务。
    """
    uri = os.getenv("TAYUNE_MILVUS_URI","").strip()
    if not uri:
        raise ValueError("请先配置 TAYUNE_MILVUS_URI，不能默认连接课堂 Milvus")

    parsed = urlparse(uri)
    if parsed.scheme not in ("http","https") or not parsed.hostname:
        raise ValueError("TAYUNE_MILVUS_URI 必须是有效的 HTTP/HTTPS 服务地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Milvus 地址中不要包含凭据、查询参数或片段；凭据请放入专属 TOKEN")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("Milvus 端口无效") from error
    # 19530 是课堂服务使用过的本机端口。改集合名不等于隔离服务
    if (
            parsed.hostname in ("localhost", "127.0.0.1", "::1")
            and port in (19530, 19531)
    ):
        raise ValueError("拒绝连接课堂端口，请使用 TAYUNE 的 19532 端口")
    # 这是人工核实后的确认标记，不是自动证明服务独立的检测器。
    if os.getenv("TAYUNE_MILVUS_ISOLATED", "").strip().lower()!="true":
        raise ValueError("请先核实服务独立，再设置 TAYUNE_MILVUS_ISOLATED=true")
    return {
        "uri": uri,
        "token": os.getenv("TAYUNE_MILVUS_TOKEN", ""),
        "db_name": os.getenv("TAYUNE_MILVUS_DB_NAME", "default"),
        "timeout": 15,
    }

def connect_client():
    """未配置就报错；不自动加载 .env，不启动数据库。"""
    options = get_connection_options()
    from pymilvus import MilvusClient

    return MilvusClient(**options)


def validate_chunks(chunks:list[Document]) -> None:
    """
    写入前校验（Pre-write Validation）函数
    全量校验通过后再写入，避免可提前发现的数据错误造成部分入库。"""
    if not chunks:
        raise ValueError("没有可入库的片段")
    seen_ids = set()
    for chunk in chunks:
        if not chunk.page_content.strip():
            raise ValueError("片段正文为空")
        if len(chunk.page_content.encode("utf-8")) > 60000:
            raise ValueError("片段正文过长，请重新切块")
        chunk_id = chunk.metadata.get("chunk_id")
        if not isinstance(chunk_id,str) or not chunk_id or len(chunk_id.encode("utf-8")) > 128:
            raise ValueError("chunk_id 必须是 1–128 字节的字符串")
        if chunk_id in seen_ids:
            raise ValueError(f"重复片段编号:{chunk_id}")
        seen_ids.add(chunk_id)
        if chunk.metadata.get("review_status") != "approved":
            raise ValueError(f"片段尚未审核通过: {chunk_id}")
        if chunk.metadata.get("source_type") not in ("article", "brewing", "policy"):
            raise ValueError("片段类型无效")
        if not chunk.metadata.get("source_url") and not chunk.metadata.get("source_file"):
            raise ValueError("片段缺少来源")

def validate_vector(vector:list[float]) -> None:
    if len(vector) != DIMENSION:
        raise ValueError("必须使用 1024 维 BGE-M3 稠密向量")
    for value in vector:
        if not math.isfinite(value):
            raise ValueError("向量包含无效数值")


class TeaVectorStore:
    def __init__(self,client,embeddings:Embeddings):
        # 外部传入客户端和模型，既可以复用连接，也方便测试时替换成模拟对象。
        self.client = client
        self.embeddings = embeddings

    def prepare_collection(self,*,create:bool = False) -> None:
        """检索时只连接已有集合；建库时才允许创建。"""
        exists = self.client.has_collection(collection_name=COLLECTION)
        if exists:
            description = self.client.describe_collection(collection_name=COLLECTION)
            if description.get("description") != COLLECTION_DESCRIPTION:
                raise ValueError("同名集合不属于本参考版,拒绝操作;请不要删除原集合")
            # 描述标签用于区分模型空间，维度检查用于避免写入不兼容向量。
            vector_field = None
            for field in description["fields"]:
                if field["name"] == "vector":
                    vector_field = field
            if vector_field is None or int(vector_field["params"]["dim"]) != DIMENSION:
                raise ValueError("集合向量维度不符合预期")
        elif create:
            from pymilvus import DataType

            # 明确建表：id 是字符串主键，vector 是向量，其余字段存动态字段。
            schema = self.client.create_schema(
                auto_id = False,enable_dynamic_field = True, description=COLLECTION_DESCRIPTION
            )
            schema.add_field(field_name="id",datatype=DataType.VARCHAR,is_primary=True, max_length=128)
            schema.add_field(field_name="vector",datatype=DataType.FLOAT_VECTOR,dim=DIMENSION)
            # FLAT 适合当前小数据集，先做精确检索基线，之后再比较近似索引。
            index_params = self.client.prepare_index_params()
            index_params.add_index(field_name="vector",index_type="FLAT", metric_type="COSINE")
            self.client.create_collection(
                collection_name=COLLECTION,
                schema=schema,
                index_params = index_params,
                consistency_level = "Strong"
            )
        else:
            raise ValueError("参考集合不存在,请先完成建库")
        self.client.load_collection(collection_name=COLLECTION)

    def add_documents(self,chunks:list[Document],batch_size:int = 8) -> int:
        """每批生成完整向量，再按 chunk_id 写入；不使用预览文件里的前 8 个数。"""
        if type(batch_size) is not int or batch_size <= 0:
            raise ValueError("batch_size 必须是正整数")
        validate_chunks(chunks)
        written = 0
        for start in range(0,len(chunks), batch_size):
            batch = chunks[start:start + batch_size]
            texts = []
            for chunk in batch:
                texts.append(chunk.page_content)
            vectors = self.embeddings.embed_documents(texts)
            if len(vectors) != len(batch):
                raise ValueError("向量数量与片段数量不一致")
            rows = []
            for chunk, vector in zip(batch,vectors):
                validate_vector(vector)
                rows.append({
                    "id": chunk.metadata["chunk_id"],
                    "vector": vector,
                    "text": chunk.page_content,
                    "metadata": chunk.metadata,
                    "review_status": chunk.metadata["review_status"],
                    "source_type": chunk.metadata["source_type"],
                })
            # upsert：主键存在则更新，不存在则插入。重复导入相同片段不会新增副本。
            result = self.client.upsert(collection_name=COLLECTION,data=rows,timeout=60)
            if result.get("upsert_count") != len(rows):
                raise RuntimeError("本批入库数量异常，请检查 Milvus 返回结果")
            written += len(rows)
        # 分批写入不是事务，中途失败可能留下已写入批次；原样重跑可补齐。
        return written


    def similarity_search_with_score(
            self, query:str, k:int =3, source_type: str | None = None
    ) -> list[tuple[Document,float]]:
        """返回 LangChain Document 与余弦分数；分数不是正确概率。"""
        if not isinstance(query,str) or not query.strip():
            raise ValueError("查询不能为空")
        if type(k) is not int or not 1 <= k <=20:
            raise ValueError("k 必须是 1–20 的整数")
        expression = 'review_status == "approved"'
        if source_type is not None:
            if source_type not in ("article", "brewing", "policy"):
                raise ValueError("无效的知识类型")
            # 只把固定枚举拼进过滤条件，不拼接任意用户输入。
            expression += f' and source_type == "{source_type}"'
        vector = self.embeddings.embed_query(query)
        validate_vector(vector)
        results = self.client.search(
            collection_name=COLLECTION,
            data=[vector],
            anns_field="vector",
            filter=expression,
            limit=k,
            search_params={"metric_type": "COSINE", "params": {}},
            output_fields=["text", "metadata"],
            timeout=30,
        )
        documents = []
        if not results:
            return documents
        # data 中只有一个查询向量，因此取第 0 组检索结果。
        for hit in results[0]:
            entity = hit["entity"]
            document = Document(page_content=entity["text"], metadata=entity["metadata"])
            documents.append((document, float(hit["distance"])))
        return documents
