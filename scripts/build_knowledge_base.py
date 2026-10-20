"""离线构建知识库的命令行脚本。
先做校验，再选择性写进 Milvus 的“离线建库脚本”
TODO：
1. 加载资料。
2. 校验 metadata。
3. 清洗与切块。
4. 批量生成向量。
5. 增量写入 Milvus/Zilliz。
6. 输出成功、跳过和失败数量。

不要在 import 时自动执行；使用 main 函数和入口保护。
"""

import argparse
import json
from pathlib import Path

from langchain_core.documents import Document

from app.core.logger import setup_logging
from app.rag.embeddings import LocalBGEmbeddings
from app.rag.vector_store import TeaVectorStore,connect_client,validate_chunks

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INPUT_FILE = PROJECT_ROOT / "data/generated/chunks.jsonl"
MODEL_PATH = Path("/Users/steven/PycharmProjects/integrated_qa_system/rag_qa/models/bge-m3")

def read_chunks(input_file:Path) -> list[Document]:
    chunks = []
    with input_file.open(encoding="utf-8") as file:
        for line_number , line in enumerate(file,start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if not isinstance(record,dict):
                    raise ValueError("每行必须是对象")
                chunk = Document(page_content=record["page_content"],metadata=record["metadata"])
            except (ValueError,KeyError,TypeError) as error:
                raise ValueError(f"{input_file.name} 第 {line_number} 行片段格式无效") from error
            chunks.append(chunk)
    validate_chunks(chunks)
    return chunks

def main() -> int:
    parser = argparse.ArgumentParser(description="校验或写入TAYUNE 参考知识库")
    parser.add_argument("--write", action="store_true",help="明确允许写入参考集合")
    args = parser.parse_args()
    logger = setup_logging(PROJECT_ROOT / "logs/build_knowledge_base.log")
    client = None

    try:
        chunks = read_chunks(INPUT_FILE)
        logger.info("片段校验通过：%d 个",len(chunks))
        if not args.write:
            logger.info ("仅校验完成，未连接 Milvus、未加载模型；加 --write 才写入")
            return 0
        client = connect_client()
        logger.info("已连接 Milvus，开始加载本地 BGE-M3")
        embeddings = LocalBGEmbeddings(MODEL_PATH)
        store = TeaVectorStore(client,embeddings)
        store.prepare_collection(create=True)
        count = store.add_documents(chunks)
        logger.info("入库完成: %d 个片段",count)
        return 0
    except Exception:
        logger.exception("建库失败；若部分批次成功，相同输入可重跑，不会自动回滚")
        return 1
    finally:
        if client is not None:
            client.close()

if __name__ == '__main__':
    raise SystemExit(main())