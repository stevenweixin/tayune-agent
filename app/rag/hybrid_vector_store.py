"""
在 Milvus 里准备一个能同时存 dense + sparse 的集合，并把文档连同两种向量一起批量写进去。
"""
from langchain_core.documents import Document
from pymilvus import DataType

from app.rag.hybrid_embeddings import HybridBGEmbeddings
from app.rag.vector_store import validate_chunks

COLLECTION = "tayune_bgem3_hybrid_v1"
DESCRIPTION = "TAYUNE BGE-M3 FlagEmbedding dense+sparse v1"


class HybridTeaVectorStore:
    def __init__(self,client,embeddings:HybridBGEmbeddings):
        self.client = client
        self.embeddings = embeddings

    def prepare_collection(self,* , create:bool =False) -> None:
        """已有集合检查后加载；只有 create=True 才允许创建新集合。"""
        if self.client.has_collection(collection_name=COLLECTION):
            description = self.client.describe_collection(collection_name=COLLECTION)
            if description.get("description") != DESCRIPTION:
                raise ValueError("同名集合不是本模块的双向量集合，拒绝使用，不自动删除")
            # 在数据库入口核对结构，避免把旧稠密集合当成双向量集合。
            fields = {}
            for field in description["fields"]:
                fields[field["name"]] = field
            dense = fields.get("dense_vector",{})
            sparse = fields.get("sparse_vector",{})
            if dense.get("type")!= DataType.FLOAT_VECTOR or int(dense.get("params",{}).get("dim",0)) != 1024:
                raise ValueError("集合缺少 1024 维稠密向量字段")
            if sparse.get("type")!= DataType.SPARSE_FLOAT_VECTOR:
                raise ValueError("集合缺少稀疏向量字段")
        elif create:
            schema = self.client.create_schema(
                auto_id = False,enable_dynamic_field=True,description=DESCRIPTION
            )
            schema.add_field(field_name="id", datatype=DataType.VARCHAR,is_primary=True,max_length=128)
            schema.add_field(field_name="dense_vector",datatype=DataType.FLOAT_VECTOR,dim=1024)
            # 稀疏字段不指定 dim；每条记录用 {token_id: weight} 保存非零位置。
            schema.add_field(field_name="sparse_vector",datatype=DataType.SPARSE_FLOAT_VECTOR)

            indexes = self.client.prepare_index_params()
            # 当前数据少，保留已有 FLAT 基线；暂不引入 HNSW 调参。
            indexes.add_index(field_name="dense_vector",index_type="FLAT",metric_type="COSINE")
            indexes.add_index(
                field_name="sparse_vector",index_type="SPARSE_INVERTED_INDEX",
                metric_type="IP",params={"drop_ratio_build":0.0},
            )
            self.client.create_collection(
                collection_name=COLLECTION,schema=schema,
                index_params=indexes,consistency_level="Strong",
            )
        else:
            raise ValueError("双向量集合不存在，请先运行对应建库入口")
        self.client.load_collection(collection_name=COLLECTION)

    def add_documents(self,chunks:list[Document],batch_size:int =4) -> int:
        """正文列表交给双向量模型，再把两种向量与原片段一起写入。"""
        if type(batch_size) is not int or batch_size<=0:
            raise ValueError("batch_size 必须为正整数")
        validate_chunks(chunks)
        written  = 0
        for start in range(0,len(chunks),batch_size):
            batch = chunks[start:start + batch_size]
            texts = []
            for chunk in batch:
                texts.append(chunk.page_content)
            dense_vectors, sparse_vectors = self.embeddings.encode(texts)

            rows = []
            # HybridBGEmbeddings 已检查数量、维度、数值并转成 Python 类型，这里不重复。
            for chunk, dense, sparse in zip(batch,dense_vectors,sparse_vectors):
                # 当前项目选择拒绝无词项的文档，防止双路索引中悄悄遗漏它。
                # 这是本版入库规则，不代表所有 Milvus 版本都禁止空稀疏向量。
                if not sparse:
                    raise ValueError(f"片段没有系数词项,请检查正文:{chunk.metadata['chunk_id']}")
                rows.append({
                    "id": chunk.metadata["chunk_id"],
                    "dense_vector": dense,
                    "sparse_vector": sparse,
                    "text":chunk.page_content,
                    "metadata":chunk.metadata,
                    "review_status":chunk.metadata["review_status"],
                    "source_type":chunk.metadata["source_type"],
                })
            result = self.client.upsert(collection_name =COLLECTION,data=rows,timeout=60)
            if result.get("upsert_count") != len(rows):
                raise RuntimeError("本批双向量入库数量异常")
            written += len(rows)
        return written