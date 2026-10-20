"""
模拟模型只验证接口和校验逻辑，不能证明真实 BGE-M3 的语义检索质量。
patch 临时替代模型依赖，测试结束后自动还原，不需要安装或下载大模型。
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.rag.embeddings import LocalBGEmbeddings

class EmbeddingTests(unittest.TestCase):
    def setUp(self):
        self.model = MagicMock()
        self.model.get_sentence_embedding_dimension.return_value = 1024
        self.model.max_seq_length = 8192
        self.model.tokenizer.encode.return_value = [1,2,3]
        factory = MagicMock(return_value = self.model)
        fake_model = SimpleNamespace(SentenceTransformer=factory)
        with patch.dict(sys.modules,{"sentence_transformers":fake_model}):
            self.embeddings = LocalBGEmbeddings(Path.cwd())
        factory.assert_called_once()
        self.assertTrue(factory.call_args.kwargs["local_files_only"])

    def test_document_vectors(self):
        self.model.encode.return_value.tolist.return_value = [[0.1]*1024,[0.2]*1024]
        vectors = self.embeddings.embed_documents(["茶知识","冲泡方法"])
        self.assertEqual(len(vectors),2)
        self.assertEqual(vectors[0][0],0.1)
        self.assertEqual(vectors[1][0],0.2)
        self.assertTrue(self.model.encode.call_args.kwargs["normalize_embeddings"])

    def test_query_vector(self):
        self.model.encode.return_value.tolist.return_value = [[0.1]* 1024]
        vector = self.embeddings.embed_query("怎么泡茶？")
        self.assertEqual(len(vector),1024)
        self.model.encode.assert_called_once()

    def test_empty_and_invalid_text(self):
        self.assertEqual(self.embeddings.embed_documents([]),[])
        for texts in ([" "],[None],"not a list"):
            with self.assertRaises(ValueError):
                self.embeddings.embed_documents(texts)
        self.model.encode.assert_not_called()

    def test_long_text(self):
        self.model.tokenizer.encode.return_value = [1] * 8193
        with self.assertRaises(ValueError):
            self.embeddings.embed_query("过长文本")
        self.model.encode.assert_not_called()

    def test_invalid_model_output(self):
        for output in ([],[[0.1]*10],[[float("nan")] * 1024]):
            self.model.encode.return_value.tolist.return_value = output
            with self.assertRaises(ValueError):
                self.embeddings.embed_query("茶")

if __name__ == '__main__':
    unittest.main()