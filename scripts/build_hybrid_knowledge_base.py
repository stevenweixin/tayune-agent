"""双向量建库：默认只校验；--write构建不可变候选，发布由独立命令确认。

项目根目录运行：python -m scripts.build_hybrid_knowledge_base
新内容不直接upsert进当前服务集合，避免内容哈希变化后遗留旧片段。
"""

import argparse
import os
from pathlib import Path

from app.core.logger import setup_logging
from app.rag.hybrid_embeddings import HybridBGEmbeddings
from app.rag.hybrid_vector_store import COLLECTION
from app.rag.vector_store import connect_client
from scripts.build_knowledge_base import read_chunks,INPUT_FILE,MODEL_PATH,PROJECT_ROOT

def main() -> int:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description="校验或写入 TAYUNE 双向量知识库")
    parser.add_argument("--write",action="store_true",help="明确允许写入双向量集合")
    parser.add_argument("--embedding-revision", default=os.getenv("TAYUNE_EMBEDDING_REVISION"),
                        help="记录本地BGE-M3模型制品版本；只构建候选，不覆盖当前集合")
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

        if not args.embedding_revision:
            raise ValueError("写入需要--embedding-revision或TAYUNE_EMBEDDING_REVISION")
        client = connect_client()
        logger.info("已连接 TAYUNE Milvus, 开始加载双向量 BGE-M3")
        embeddings = HybridBGEmbeddings(Path(os.getenv("TAYUNE_BGE_MODEL_PATH", str(MODEL_PATH))))
        from app.rag.knowledge_releases import KnowledgeReleases
        releases = KnowledgeReleases(PROJECT_ROOT / "data/generated/knowledge_releases")
        manifest = releases.stage(client, embeddings, chunks, args.embedding_revision)
        logger.info("候选版本已校验：%s，%d个片段；未切换线上知识库", manifest["id"], manifest["count"])
        logger.info("发布：python -m scripts.manage_knowledge_release activate --release %s --write", manifest["id"])
        return 0
    except Exception:
        logger.exception("双向量建库失败；部分批次可能已写入，不自动回滚或删除集合")
        return 1
    finally:
        if client is not None:
            client.close()

if __name__ == '__main__':
    raise SystemExit(main())

