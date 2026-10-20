"""Agent 状态定义。

TODO：
1. 先画出每个节点真正需要的数据。
2. 定义用户问题、意图、需求条件、商品、文档、答案和错误字段。
3. 需要累加的列表再使用 Reducer，不要所有字段都累加。
4. 避免把第三方客户端对象放进 State。
"""

from typing import TypedDict
from langchain_core.messages import BaseMessage

class TeaState(TypedDict,total=False):
    # 本版节点返回完整messages列表，所以使用覆盖语义，不能再加add_messages。
    # 图只处理一次请求，会话历史由服务层SQLite保存，避免旧工具消息无限累积。
    messages: list[BaseMessage]
    answer: str
    summary: str
    tool_count: int
    model_count: int
    input_tokens: int
    output_tokens: int
    known_product_ids: list[int]
    sources: list[dict]
    trace: list[str]
    degraded: bool

