"""单次问答的消息、预算、计划、证据和恢复状态；不存第三方客户端。"""

from typing import TypedDict
from langchain_core.messages import BaseMessage

class TeaState(TypedDict, total=False):
    # 本版节点返回完整messages列表，所以使用覆盖语义，不能再加add_messages。
    # 图只处理一次请求，会话历史由服务层SQLite保存，避免旧工具消息无限累积。
    messages: list[BaseMessage]
    answer: str
    answer_parts: list[dict]
    summary: str
    tool_count: int
    model_count: int
    # 本轮最终回答被门禁退回的次数；最多一次，不是工具重试次数。
    # 每次 graph.ainvoke 都是新 State，graph 使用 get(..., 0)，服务层不用改。
    answer_repair_count: int
    input_tokens: int
    output_tokens: int
    known_product_ids: list[int]
    sources: list[dict]
    trace: list[str]
    degraded: bool
    plan: dict
    resume_node: str
    dialogue: list[dict]
    evidence_started_at: float
    goal: str
    constraints: dict
    evidence_ids: list[str]
    session_revision: float
    memory_revision: int
