"""
unittest 是标准库，Document 与切分器来自 LangChain；测试不修改正式资料。

test_conversion
→ 字典能不能正确转成 Document？
→ metadata copy 有没有生效？

test_short_document
→ 短文章会不会被乱切？

test_positions_and_ids
→ chunk 位置对不对？
→ ID 唯不唯一？
→ 来源有没有继承？
→ 原文有没有漏掉？

test_invalid_input
→ 错误输入会不会被正确拒绝？
"""

import unittest
from langchain_core.documents import Document
from app.rag.chunking import to_documents,chunk_documents

class ChunkingTests(unittest.TestCase):
    def test_conversion(self):
        """
        ① to_documents() 真的返回 Document
        ② 新 Document 的 metadata 和原字典相互独立
        """
        record = {"page_content":"茶知识","metadata":{"document_id":"demo"}}
        documents = to_documents([record])
        self.assertIsInstance(documents[0],Document)
        documents[0].metadata["title"] = "new"
        self.assertNotIn("title",record["metadata"])

    def test_short_document(self):
        """
        短文章
            ↓
        不要乱拆
            ↓
        不要修改正文
        """
        document = Document(page_content="短文不用拆分", metadata={"document_id": "demo"})
        chunks = chunk_documents([document],100,10)
        self.assertEqual(len(chunks),1)
        self.assertEqual(chunks[0].page_content,document.page_content)

    def test_positions_and_ids(self):
        text = "茶知识正文。English sentence. \n\n冲泡步骤需要保留。" * 20
        document = Document(page_content=text,metadata={"document_id":"demo","source_url":"https://example.com"})
        chunks = chunk_documents([document],60,10)
        self.assertEqual(chunks,chunk_documents([document],60,10))
        covered = set()
        seen_ids = set()
        for chunk in chunks:
            metadata = chunk.metadata
            start = metadata["start_char"]
            end = metadata["end_char"]
            self.assertEqual(chunk.page_content,text[start:end])
            self.assertLessEqual(len(chunk.page_content),60)
            self.assertEqual(metadata["parent_document_id"],"demo")
            self.assertEqual(metadata["source_url"],"https://example.com")
            self.assertNotIn(metadata["chunk_id"],seen_ids)
            seen_ids.add(metadata["chunk_id"])
            covered.update(range(start,end))
        for position, character in enumerate(text):
            if not character.isspace():
                self.assertIn(position,covered)
        self.assertNotIn("chunk_id",document.metadata)

    def test_invalid_input(self):
        for size, overlap in ((0,0),(10,-1),(10,10),(True,0)):
            with self.assertRaises(ValueError):
                chunk_documents([],size,overlap)
        with self.assertRaises(ValueError):
            to_documents([{"page_content":" ","metadata":{"document_id":"empty"}}])
        document = Document(page_content= "text",metadata = {"document_id":"demo"})
        with self.assertRaises(ValueError):
            chunk_documents([document,document])

if __name__ == '__main__':
    unittest.main()
