"""在原文本客户端之外新增工具调用，保留generate_text供摘要使用。
模型返回AIMessage，其tool_calls需要代码执行，不会由模型自己访问网站。

核心功能:让模型决定要不要调用工具
调用模型的那一层封装,做三件事:
调用前检查消息是否超限(控制成本)
调用时让模型知道有哪些工具,并限制等待时间
调用后检查模型返回的东西是否合格,不合格就报错,不放行
"""
import asyncio
import json
from langchain_core.messages import AIMessage


def check_messages(messages):
    if not messages or len(messages) > 30:
        raise ValueError("Agent消息数量超限")
    total = 0
    for message in messages:
        if not isinstance(message.content, str):
            raise ValueError("仅支持文本消息")
        total += len(message.content)
        if isinstance(message, AIMessage):
            total += len(json.dumps(message.tool_calls))
    if total > 24000:
        raise ValueError("Agent输入超过24000字符预算")


class ToolModel:
    def __init__(self, llm, tools):
        # 默认auto选择；不强制每条消息调用工具，允许追问。
        self.bound = llm.bind_tools(tools)
        # 把每个 Tool 的 name、description、参数 schema 转成 JSON,附在每次请求里发给模型。

    async def ask(self, messages):
        check_messages(messages)
        try:
            response = await asyncio.wait_for(self.bound.ainvoke(messages), timeout=30)
        except Exception:
            raise RuntimeError("模型服务暂不可用") from None
        if not isinstance(response, AIMessage) or response.invalid_tool_calls:
            raise RuntimeError("模型工具参数格式无效")
        finish = response.response_metadata.get("finish_reason")
        if finish not in ("stop", "tool_calls"):
            raise RuntimeError("模型输出未正常结束")
        if not response.tool_calls and (not isinstance(response.content, str) or not response.content.strip()):
            #既没有工具请求, 文字内容也是空的。这种情况模型等于什么都没说,应当报错。
            raise RuntimeError("模型未返回回答或工具请求")
        return response


