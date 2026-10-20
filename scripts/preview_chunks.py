"""离线切块预览。运行：python -m scripts.preview_chunks_1
todo 切块质检工具
读取已完成的 loader，输出片段和完整可读预览，不调用模型或数据库。
再次执行会更新参考结果，不修改原始资料和正式输出。
"""

from pathlib import Path
from langchain_core.documents import Document

from app.core.logger import setup_logging
from app.rag.chunking import chunk_documents,to_documents
from app.rag.loaders import INPUT_FILES,load_documents
from scripts.normalize_articles import write_jsonl

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_FILE = PROJECT_ROOT / "data/generated/chunks.jsonl"
PREVIEW_FILE = PROJECT_ROOT / "data/generated/chunks_preview.md"

def write_preview(chunks:list[Document],output_file:Path) -> None:
    """把每块的标题、来源、位置与正文写出，检查有没有切断重要内容。"""
    output_file.parent.mkdir(parents=True,exist_ok=True)
    with output_file.open("w", encoding="utf-8") as file:
        file.write("# 切块预览\n\n长度单位为字符,请重点检查表格,步骤和引用. \n\n")
        for number, chunk in enumerate(chunks,start=1):
            metadata = chunk.metadata
            source = metadata.get("source_url") or metadata.get("source_file")
            file.write(f"## 片段{number}:{metadata['title']}\n\n")
            file.write(f"- 来源：{source}\n")
            file.write(f"- 父文档：{metadata['parent_document_id']}\n")
            file.write(f"- 文内编号：{metadata['chunk_index']}（从 0 开始）\n")
            file.write(f"- 字符范围：[{metadata['start_char']}, {metadata['end_char']})\n")
            file.write(f"- 字符数：{metadata['char_count']}\n\n")
            file.write(chunk.page_content)
            file.write("\n\n---\n\n")

def main() -> int:
    logger = setup_logging(PROJECT_ROOT / "logs/preview_chunks.log")
    try:
        records = load_documents(INPUT_FILES)
        if not records:
            raise ValueError("没有可切块的已审核文档")
        documents = to_documents(records)
        chunks = chunk_documents(documents,chunk_size=800,chunk_overlap=100)
        # Document 不是普通字典，写 JSON 前要转回可序列化格式。
        chunk_records = []
        for chunk in chunks:
                chunk_records.append({"page_content":chunk.page_content,"metadata":chunk.metadata})
        write_jsonl(chunk_records, OUTPUT_FILE)
        write_preview(chunks,PREVIEW_FILE)
        logger.info("切块完成: %d 份文档 -> %d 个片段", len(documents), len(chunks))
        logger.info("片段文件: %s", OUTPUT_FILE)
        logger.info("可读预览：%s", PREVIEW_FILE)
        return 0
    except Exception:
        logger.exception("切块预览失败")
        return 1

if __name__ == '__main__':
    raise SystemExit(main())