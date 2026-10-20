"""
默认只试算前 3 个片段和一个问题，先确认接口，不执行全库入库。
首次加载本地 BGE-M3 需要时间和内存；不会下载新模型或调用付费 API。
"""
import json
from pathlib import Path

from app.core.logger import setup_logging
from app.rag.embeddings import LocalBGEmbeddings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODEL_PATH = Path("/Users/steven/PycharmProjects/integrated_qa_system/rag_qa/models/bge-m3")
INPUT_FILE = PROJECT_ROOT / "data/generated/chunks.jsonl"
OUTPUT_FILE = PROJECT_ROOT / "data/generated/embedding_preview.json"
SAMPLE_SIZE = 3

def read_sample(input_file:Path,limit:int) -> list[dict]:
    """读取少量真实片段，不把整个片段文件都加载到模型里。"""
    if type(limit) is not int or limit <= 0:
        raise ValueError("样本数必须为正整数")
    samples = []
    with input_file.open(encoding="utf-8") as file:
        for line_number, line in enumerate(file,start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record,dict):
                raise ValueError(f"第{line_number}行必须是对象")
            content = record.get("page_content")
            metadata = record.get("metadata")
            if not isinstance(content,str) or not content.strip():
                raise ValueError(f"第 {line_number} 行缺少正文")
            if not isinstance(metadata,dict) or not metadata.get("chunk_id"):
                raise ValueError(f"第 {line_number} 行缺少片段编号")
            samples.append(record)
            if len(samples) == limit:
                break
    if not samples:
        raise ValueError("片段文件为空")
    return samples


def main() -> int:
    logger = setup_logging(PROJECT_ROOT / "logs/preview_embeddings.log")
    try:
        samples= read_sample(INPUT_FILE,SAMPLE_SIZE)
        logger.info("开始加载本地 BGE-M3（CPU），首次加载可能较慢")
        embeddings = LocalBGEmbeddings(MODEL_PATH)
        texts = []
        for sample in samples:
            texts.append(sample["page_content"])
        vectors = embeddings.embed_documents(texts)
        query = "How should I brew Phoenix Dan Cong tea?"
        query_vector = embeddings.embed_query(query)

        preview = []
        for sample,vector in zip(samples,vectors):
            # 只展示前 8 个数；这不是可用于入库的完整向量文件。
            preview.append({
                "chunk_id": sample["metadata"]["chunk_id"],
                "text_preview": sample["page_content"][:120],
                "dimension": len(vector),
                "vector_first_8": vector[:8],
            })
        result = {
            "model_path": str(MODEL_PATH),
            "normalized": True,
            "documents": preview,
            "query": query,
            "query_dimension": len(query_vector),
            "query_vector_first_8": query_vector[:8],
        }
        OUTPUT_FILE.parent.mkdir(parents=True,exist_ok=True)
        with OUTPUT_FILE.open("w",encoding="utf-8") as file:
            json.dump(result,file,ensure_ascii=False,indent=2,allow_nan=False)
        logger.info("预览完成：%d 个文档向量，维度 %d；输出 %s", len(vectors), embeddings.dimension, OUTPUT_FILE)
        return 0
    except Exception:
        logger.exception("向量预览失败")
        return 1
if __name__ == '__main__':
    raise SystemExit(main())