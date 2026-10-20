"""双向量建库参考入口：默认只校验，--write 才创建集合并写入。

读取已有片段 → 加载双向量模型 → 准备新集合
→ 同时写入 dense、sparse 和正文 → 记录数量 → 关闭连接

项目根目录运行：python -m scripts.build_hybrid_knowledge_base_1
本参考入口写参考集合；跟写正式版时同步修改存储模块的导入。
"""

import argparse

from app.core.logger import setup_logging
from app.rag.hybrid_embeddings import HybridBGEmbeddings
from app.rag.hybrid_vector_store import HybridTeaVectorStore,COLLECTION
from app.rag.vector_store import connect_client
from scripts.build_knowledge_base import read_chunks,INPUT_FILE,MODEL_PATH,PROJECT_ROOT

def main() -> int:
    parser = argparse.ArgumentParser(description="校验或写入 TAYUNE 双向量知识库")
    parser.add_argument("--write",action="store_true",help="明确允许写入双向量集合")
    args = parser.parse_args()
    logger = setup_logging(PROJECT_ROOT / "logs/build_hybrid_knowledge_base.log")
    client = None
    try:
        # 复用已经验证过的 JSONL 读取和片段校验，不重新分块。
        chunks = read_chunks(INPUT_FILE)
        logger.info("片段校验通过：%d 个；目标集合：%s", len(chunks), COLLECTION)
        if not args.write:
            logger.info("仅校验完成，未连接 Milvus、未加载模型；加 --write 才写入")
            return 0

        client = connect_client()
        logger.info("已连接 TAYUNE Milvus, 开始加载双向量 BGE-M3")
        embeddings = HybridBGEmbeddings(MODEL_PATH)
        store = HybridTeaVectorStore(client, embeddings)
        store.prepare_collection(create=True)
        # add_documents 内部调用 encode，一次得到 dense 和 sparse 并配对写入。
        count = store.add_documents(chunks)
        logger.info("双向量入库完成：%d 个片段；集合：%s", count, COLLECTION)
        return 0
    except Exception:
        logger.exception("双向量建库失败；部分批次可能已写入，不自动回滚或删除集合")
        return 1
    finally:
        if client is not None:
            client.close()

if __name__ == '__main__':
    raise SystemExit(main())

