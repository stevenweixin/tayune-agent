"""Embedding 模型适配层。

TODO：
1. 定义文档向量化和查询向量化的统一接口。
2. 两者必须使用兼容的同一模型与维度。
3. 本地 BGE-M3 与在线轻量模型通过配置切换。
4. 批量生成向量，避免逐条调用。

LangChain Embeddings 参考实现：本地 BGE-M3 的稠密向量。
Embeddings 是 LangChain 的接口，SentenceTransformer 是模型运行库。
此处不生成稀疏向量，不调用 DeepSeek，不下载模型，也不写入 Milvus。
"""
import math
from pathlib import Path

from langchain_core.embeddings import Embeddings

class LocalBGEmbeddings(Embeddings):
    """实现 LangChain 规定的文档, 问题两个向量化方法"""
    def __init__(self,model_path:Path,batch_size:int = 4):
        if not model_path.is_dir():
            raise ValueError(f"本地模型目录不存储:{model_path}")
        if type(batch_size) is not int or batch_size <=0:
            raise ValueError(f"batch_size 必须为正整数")
        # 延迟导入: 只在正整创建模型时加载重型依赖,普通代入和模拟测试无需它
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as error:
            raise ImportError("请先在 tayune_agent 环境安装 sentence-transformers 和 torch，见本组说明文档") from error

        self.model_path = model_path.resolve()
        self.batch_size = batch_size
        self.model = SentenceTransformer(
            str(self.model_path),
            device="cpu",
            local_files_only=True,
            trust_remote_code=False,
        )
        # 首版用 CPU ；之后根据机器资源再测试加速方案。
        self.dimension = self.model.get_sentence_embedding_dimension()
        if self.dimension != 1024:
            raise ValueError("本参考版要求 BGE-M3，预期稠密向量维度为 1024")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """LangChain 接口：输入多个片段，按输入顺序返回多个向量。"""
        if not isinstance(texts,list):
            raise ValueError("texts 必须是字符串列表")
        if not texts:
            return []
        for text in texts:
            if not isinstance(text,str) or not text.strip():
                raise ValueError("不能向量化空正文或非字符串")
            # SentenceTransformer 可能自动截断长文本；这里提前拒绝，避免悄悄丢尾部。
            token_ids = self.model.tokenizer.encode(text,add_special_tokens=True,truncation=False)
            if len(token_ids) > self.model.max_seq_length:
                raise ValueError("文本超过模型 Token 上线,请先减少切块长度")

        vectors = self.model.encode(
            texts,
            batch_size=self.batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        ).tolist()
        if len(vectors) != len(texts):
            raise ValueError("模型返回的向量数量与文本数量不同")
        for vector in vectors:
            if len(vector) != self.dimension:
                raise ValueError("模型返回的向量维度不一致")
            for value in vector:
                if not math.isfinite(value):
                    raise ValueError("向量包含无效数值")
        return vectors

    def embed_query(self,text:str) -> list[float]:
        """LangChain 接口：问题也使用同一个模型，返回一个一维向量。"""
        vectors = self.embed_documents([text])
        return vectors[0]