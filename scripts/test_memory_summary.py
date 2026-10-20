"""离线测试：不会读取密钥、调用DeepSeek或连接Milvus。"""
import unittest
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from app.core import memory_summary as module


def make_history():
    return [SystemMessage(content="Tea only"),
            HumanMessage(content="Budget $30"), AIMessage(content="Noted"),
            HumanMessage(content="Actually $20"), AIMessage(content="Understood"),
            HumanMessage(content="How do I brew it?")]

class SummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_below_threshold(self):
        with patch.object(module, "generate_text", new_callable=AsyncMock) as call:
            result = await module.compress_history(make_history())
        self.assertEqual(result["status"], "unchanged")
        call.assert_not_awaited()

    async def test_success_preserves_recent_and_input(self):
        messages = make_history()
        original = list(messages)
        response = {"text": "Customer budget is $20.", "usage": {"total_tokens": 80}}
        with patch.object(module, "generate_text", new_callable=AsyncMock, return_value=response) as call:
            result = await module.compress_history(messages, "Likes light tea", 3, 1)
        self.assertEqual(result["status"], "summarized")
        self.assertEqual(result["messages"], [messages[0], messages[-1]])
        self.assertEqual(messages, original)
        prompt = call.call_args.args[0]
        self.assertIn("Likes light tea", prompt[1].content)
        self.assertNotIn("How do I brew it?", prompt[1].content)
        # 下一次只传剩余历史，不会马上再次摘要已经处理过的轮次。
        with patch.object(module, "generate_text", new_callable=AsyncMock) as call:
            second = await module.compress_history(result["messages"], result["summary"], 3, 1)
        self.assertEqual(second["status"], "unchanged")
        call.assert_not_awaited()


    async def test_service_failure_keeps_history(self):
        messages = make_history()
        with patch.object(module, "generate_text", new_callable=AsyncMock,
                          side_effect=RuntimeError("request failed")):
            result = await module.compress_history(messages, "Old summary", 3, 1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["messages"], messages)
        self.assertEqual(result["summary"], "Old summary")

    async def test_long_input_does_not_call_model(self):
        messages = make_history()
        messages[1] = HumanMessage(content="x" * 12000)
        with patch.object(module, "generate_text", new_callable=AsyncMock) as call:
            result = await module.compress_history(messages, threshold=3, keep_rounds=1)
        self.assertEqual(result["status"], "failed")
        call.assert_not_awaited()

    async def test_invalid_output_keeps_history(self):
        for text in (" ", "x" * 1601):
            with patch.object(module, "generate_text", new_callable=AsyncMock,
                              return_value={"text": text, "usage": None}):
                result = await module.compress_history(make_history(), "old", 3, 1)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["summary"], "old")

    def test_invalid_rounds_and_order(self):
        with self.assertRaises(ValueError):
            module.split_history(make_history(), threshold=2, keep_rounds=2)
        with self.assertRaises(ValueError):
            module.split_history([AIMessage(content="Orphan reply")])


if __name__ == "__main__":
    unittest.main()
