"""5.4 双向量输出补充：为 5.6 混合召回准备数据，不连接 Milvus。

保留原 embeddings.py 稠密基线；这里采用老师的 encode / encode_query 接口。
"""

import math
from pathlib import Path

class HybridBGEmbeddings:
    def __init__(self,model_path:Path,batch_size:int =4):
        if type(batch_size) is not int or batch_size <= 0:
            raise ValueError("batch_size 必须为正整数")
        # 模型路径是配置入口；缺少稀疏头时不能用随机权重代替已训练权重。
        for filename in ("config.json", "pytorch_model.bin","tokenizer.json",
                         "sparse_linear.pt","colbert_linear.pt"):
            if not (model_path / filename).is_file():
                raise ValueError(f"本地 BGE-M3 模型确实文件:{filename}")
        try:
            from FlagEmbedding import BGEM3FlagModel
        except ImportError as error:
            raise ImportError("请先按 docs/09_HYBRID_EMBEDDINGS.md 检查 FlagEmbedding 依赖")from error

        self.batch_size = batch_size
        self.max_length = 8192
        # 单个 CPU，关闭半精度；仅创建一次对象并复用，不在每次 encode 时加载模型。
        self.model = BGEM3FlagModel(
            str(model_path.resolve()),
            devices="cpu",
            use_fp16=False,
            normalize_embeddings =True,
            trust_remote_code = False,
        )

    def encode(self, texts: list[str]) -> tuple[list[list[float]], list[dict[int,float]]]:
        """输入多段正文；返回按原顺序一一对应的 dense 列表与 sparse 列表。"""
        if not isinstance(texts,list):
            raise ValueError("texts 必须是字符串列表")
        if not texts:
            return [],[]
        for  text in texts:
            if not isinstance(text,str) or not text.strip():
                raise ValueError("不能编码空文本或非字符串")
            # 项目规则:拒绝过长文本,而不是让模型悄悄截掉后面的内容
            token_ids = self.model.tokenizer.encode(text,add_special_tokens=True,truncation=False)
            if len(token_ids) > self.max_length:
                raise ValueError("文本超过 8192 Token，请先减小切块长度")

        output = self.model.encode(
            texts,
            batch_size=self.batch_size,
            max_length=self.max_length,
            return_dense=True,
            return_sparse=True,
            return_colbert_vecs=False,
        )
        # 外部模型输出在这里转换并校验，后续模块不用重复处理 NumPy 类型。
        dense_vectors = output["dense_vecs"].tolist()
        raw_sparse_vectors = output["lexical_weights"]
        if len(dense_vectors) != len(texts) or len(raw_sparse_vectors) != len(texts):
            raise ValueError("模型返回的向量数量与文本数量不一致")
        for vector in dense_vectors:
            if len(vector) != 1024:
                raise ValueError("BGE-M3 稠密向量应为 1024 维")
            for value in vector:
                if not math.isfinite(value):
                    raise ValueError("稠密向量包含无效数值")

        sparse_vectors = []
        for raw_weights in raw_sparse_vectors:
            weights = {}
            for token_id, weight in raw_weights.items():
                # FlagEmbedding 的 token ID 可能是字符串，权重可能是 NumPy 数值。
                # 转成 int / float，方便后续 Milvus 写入和状态序列化。
                token_id = int(token_id)
                weight = float(weight)
                if token_id < 0 or not math.isfinite(weight) or weight <0:
                    raise ValueError("稀疏向量包含无效索引或权重")
                if weight > 0:
                    weights[token_id] = weight
            # 空字典保留原位置,避免与正文错位; 蔚来检索入口决定空稀疏向量回退.
            sparse_vectors.append(weights)
        return dense_vectors,sparse_vectors

    def encode_query(self, text: str) -> tuple[list[float], dict[int, float]]:
        """把一个问题包成列表复用 encode，再取出唯一一条结果。"""
        dense_vectors, sparse_vectors = self.encode([text])
        return dense_vectors[0], sparse_vectors[0]

if __name__ == "__main__":
    from scripts.build_knowledge_base import MODEL_PATH

    embedder = HybridBGEmbeddings(MODEL_PATH)
    dense, sparse = embedder.encode_query("How should I brew Phoenix Dan Cong tea?")
    print("稠密向量维度:", len(dense))
    print("稀疏向量非零项数量:", len(sparse))
    print("稀疏向量前 5 项:", list(sparse.items())[:5])

