"""知识文档切块。
    LangChain 切块参考：字典 -> Document -> Document 片段。
    仅本地处理，不调用模型。长度单位是字符，不是 Token。
"""
import hashlib

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

def to_documents(records: list[dict]) -> list[Document]:
    """自己的适配函数：把 loader 的字典转换成 LangChain 文档对象。"""
    documents = []
    seen_ids = set()
    for record in records:
        content = record["page_content"]
        metadata = record["metadata"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError("正文必须是非空字符串")
        document_id = metadata["document_id"]
        if document_id in seen_ids:
            raise ValueError(f"文档 ID {document_id} 重复")
        seen_ids.add(document_id)
        # Document 是 LangChain 类型；copy 避免修改原记录的元数据。
        documents.append(Document(page_content=content, metadata=metadata.copy()))
    return documents

def chunk_documents(
        documents: list[Document], chunk_size: int = 800, chunk_overlap: int = 100
) -> list[Document]:
    """LangChain 负责切分，本函数补充项目需要的编号和来源关系"""
    if type(chunk_size) is not int or type(chunk_overlap) is not int:
        raise ValueError("切块参数必须为整数")
    if chunk_size <= 0 or chunk_overlap < 0 or chunk_overlap >= chunk_size:
        raise ValueError("切块参数不合理,要求 chunk_size > 0 且 0 <= chunk_overlap < chunk_size")

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        length_function=len,
        # 优先段落、换行、句子、空格，最后的空字符串允许按字符拆分。
        separators=["\n\n", "\n", "。", "！", "？", ". ", " ", ""],
        keep_separator=True,
        # LangChain 自动记录片段在原文中的起点，放进 metadata。
        add_start_index=True,
        strip_whitespace=False,
        is_separator_regex=False,
    )
    chunks = []
    seen_ids = set()
    for document in documents:
        document_id = document.metadata["document_id"]
        if document_id in seen_ids:
            raise ValueError(f"文档 ID {document_id} 重复")
        seen_ids.add(document_id)
        if not document.page_content.strip():
            raise ValueError("正文为空")
        # split_documents 是 LangChain API，输入与输出均为 Document 列表。
        document_chunks = splitter.split_documents([document])
        for chunk_index , chunk in enumerate(document_chunks):
            if not chunk.page_content.strip():
                continue
            start = chunk.metadata["start_index"]
            if start< 0:
                raise ValueError(f"无法定位片段的原文位置: {document_id}")
            end = start + len(chunk.page_content)
            # 相同输入重复执行得到相同编号；位置或正文变化时编号变化。
            identity = f"{document_id}:{start}:{end}:{chunk.page_content}"
            chunk_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            chunk.metadata["chunk_id"] = "chunk_" + chunk_id
            # 父文档是整篇原文,这不等于已经实现两级父子块检索.
            chunk.metadata["parent_document_id"] = document_id
            chunk.metadata["chunk_index"] = chunk_index
            chunk.metadata["start_char"] = start
            chunk.metadata["end_char"] = end
            chunk.metadata["char_count"] = len(chunk.page_content)
            chunks.append(chunk)
    # overlap 是目标值,不保证每对片段都恰好重叠这么多字符
    # 表格也可能被拆开,需要在预览中检查. 800/100 只是实验起点.
    return chunks
