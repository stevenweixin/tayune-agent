"""
接收消息列表 → 使用 LangChain 异步调用 DeepSeek → 返回回答、用量和耗时。
包含必要的超时、有限重试和错误提示，暂不接入检索、工具或完整 Agent。
"""
import unittest
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage,HumanMessage
from app.integrations import deepseek as module

class DeepSeekTests(unittest.IsolatedAsyncioTestCase):
    async def test_success(self):
        response = AIMessage(content=" Hello ", response_metadata={"finish_reason": "stop"},
                             usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12})
        llm = AsyncMock()
        llm.ainvoke.return_value = response
        with patch.object(module, "read_config", return_value={}), patch.object(module, "create_chat_model", return_value=llm):
            result = await module.generate_text([HumanMessage(content="hi")])
        self.assertEqual(result["text"], "Hello")
        self.assertEqual(result["usage"]["total_tokens"], 12)

    async def test_invalid_input(self):
        for messages in ([], [HumanMessage(content=" ")], [HumanMessage(content="x" * 12001)]):
            with self.assertRaises(ValueError):
                await module.generate_text(messages)

    async def test_truncated_output(self):
        llm = AsyncMock()
        llm.ainvoke.return_value = AIMessage(content="partial", response_metadata={"finish_reason": "length"})
        with patch.object(module, "read_config", return_value={}), patch.object(module, "create_chat_model", return_value=llm):
            with self.assertRaisesRegex(RuntimeError, "没有正常结束"):
                await module.generate_text([HumanMessage(content="hi")])

    async def test_error_redaction(self):
        llm = AsyncMock()
        llm.ainvoke.side_effect = RuntimeError("secret-value-and-customer-message")
        with patch.object(module, "read_config", return_value={}), patch.object(module, "create_chat_model", return_value=llm):
            with self.assertRaises(RuntimeError) as caught:
                await module.generate_text([HumanMessage(content="hi")])
        self.assertNotIn("secret-value", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    async def test_total_timeout(self):
        import asyncio
        async def slow(*args):
            await asyncio.sleep(1)
        llm = AsyncMock()
        llm.ainvoke.side_effect = slow
        with patch.object(module, "read_config", return_value={}), patch.object(module, "create_chat_model", return_value=llm), patch.object(module, "TOTAL_TIMEOUT", 0.001):
            with self.assertRaisesRegex(RuntimeError, "超时"):
                await module.generate_text([HumanMessage(content="hi")])

    def test_config_destination_and_precedence(self):
        with patch.object(module, "dotenv_values", return_value={"DEEPSEEK_API_KEY": "file-key"}), patch.dict(module.os.environ, {"DEEPSEEK_API_KEY": "env-key"}, clear=True):
            self.assertEqual(module.read_config()["DEEPSEEK_API_KEY"], "env-key")
        with patch.object(module, "dotenv_values", return_value={"DEEPSEEK_API_KEY": "fake", "DEEPSEEK_BASE_URL": "https://example.org"}), patch.dict(module.os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                module.read_config()


if __name__ == "__main__":
    unittest.main()