"""5.9 短期记忆参考版：会话标识 + MemorySaver + 最近N轮对话。

注意事项：
1. MemorySaver保存的是LangGraph状态检查点，不是Milvus知识库或模型权重。
   本节提供工具函数，直到后续compile(checkpointer=...)才真正参与业务图。
2. 内存检查点在进程重启后丢失，多进程也不共享，只适合当前本地实验。
3. thread_id只是存储键，不是身份认证。后端必须核验会话归属，不能信任
   浏览器随意提交的user_id或thread_id；匿名用户也要服务端分配会话。
4. 窗口只减少传给LLM的消息，不自动删除MemorySaver的历史检查点，
   也不保证Token数足够小。后续还要限制输入长度和整体上下文预算。
5. 历史消息是不可信数据，不能把用户历史或摘要提升为system权限。
6. 本节点仅支持普通文本Human/AI对话；工具消息需成组保留，后续另接。
7. 不在这里保存订单地址、密码、密钥等敏感资料；真实会话需要过期和清理。

后端确认用户身份 → build_config(user_id, session_id)
                        └─ 生成 thread_id（JSON键）

未来的图 compile(checkpointer=get_memory_saver("tea"))
   └─ 每次 invoke 带上 config → 按 thread_id 存取该会话的状态

调用 LLM 前 → trim_messages_to_window(历史消息, N)
   └─ 只把"系统消息 + 最近N轮"传给模型
"""

import json
from pathlib import Path

from langchain_core.messages import AIMessage, BaseMessage,HumanMessage,SystemMessage
from app.core.logger import setup_logging

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_memory_savers = {}

def get_memory_saver(agent_type: str = "tea"):
    """同一个Agent复用实例，避免每次调用创建新对象而丢失历史。

        建议服务启动时调用，不把动态用户输入当成agent_type来无限创建实例。
        每个Agent独立实例是本项目的隔离选择，并不是不同State必然无法共存。
     """
    if not isinstance(agent_type,str) or not agent_type.strip():
        raise ValueError("agent_type必须是非空字符串")
    if agent_type not in _memory_savers:
        # 延迟导入：未安装LangGraph时，仍可测试下面的纯消息函数。
        try:
            from langgraph.checkpoint.memory import MemorySaver
        except ImportError as error:
            raise ImportError("请在tayune_agent环境安装项目指定的langgraph==1.0.9") from error
        _memory_savers[agent_type] = MemorySaver()
    return _memory_savers[agent_type]

def build_thread_id(user_id: str, session_id: str) -> str:
    """将后端已确认的用户和会话编号组合成不易发生分隔符碰撞的存储键。"""
    for name, value in (("user_id", user_id), ("session_id", session_id)):
        if not isinstance(value,str) or not value.strip() or len(value) > 128:
            raise ValueError(f"{name}必须是1到128字符的有效编号")
    # JSON保留两部分的边界，比直接拼接带下划线的字符串更明确。
    # 这不是加密，不要传邮箱、手机号等个人信息，使用服务端内部ID。
    return json.dumps([user_id,session_id], ensure_ascii=True, separators=(",",":"))

def build_config(user_id:str, session_id:str) -> dict:
    """以后传给graph.invoke/ainvoke；configurable和thread_id是框架约定的键。"""
    return {"configurable": {"thread_id": build_thread_id(user_id,session_id)}}

def trim_messages_to_window(messages: list[BaseMessage], window_size: int = 5) -> list[BaseMessage]:
    """保留开头的系统消息，以及最近N轮；最后未回答的Human消息也算一轮。
    输入由后端构造，不能把浏览器提供的role直接变成SystemMessage。
    返回新列表，不修改传入列表；消息对象仍复用，调用方也不要原地修改它们。
    """
    if type(window_size) is not int or window_size <= 0 :
        raise ValueError("window_size必须是正整数")
    system_message = []
    rounds = []
    for message in messages:
        if not isinstance(message.content, str):
            raise ValueError("本节点只支持普通文本消息,不处理多模态内容")
        if isinstance(message,SystemMessage):
            if rounds:
                raise ValueError("系统消息只能放在开头，不把历史中的消息提升为系统指令")
            system_message.append(message)
        elif isinstance(message,HumanMessage):
            if rounds and len(rounds[-1]) == 1:
                raise ValueError("连续出现两个用户消息,请先确认对话轮次")
            rounds.append([message])
        elif isinstance(message, AIMessage):
            if message.tool_calls or message.invalid_tool_calls or message.additional_kwargs.get("tool_calls"):
                raise ValueError("工具调用需要配对保留,本节点不处理工具消息")
            if not rounds or len(rounds[-1]) != 1:
                raise ValueError("AI消息必须跟子对应的用户消息之后")
            rounds[-1].append(message)
        else:
            raise ValueError("本节点只接受SystemMessage、HumanMessage和普通AIMessage")

    result = list(system_message)
    # 按轮而不是简单取最后2*N条，避免末尾有新问题时留下孤立的旧AI回答。
    for dialogue_round in rounds[-window_size:]:
        result.extend(dialogue_round)
    return result

def main() -> int:
    """直接Run，使用固定演示对话；不调用LLM、不访问真实客户历史。"""
    logger = setup_logging(PROJECT_ROOT / "logs/memory.log")
    try:
        config = build_config("demo-user","demo-session")
        saver = get_memory_saver("tea")
        assert saver is get_memory_saver("tea")
        messages = [SystemMessage(content="You are a tea shopping assistant.")]
        for number in range(1,5):
            messages.append(HumanMessage(content=f"Demo tea question {number}"))
            messages.append(AIMessage(content=f"Demo answer {number}"))
        messages.append(HumanMessage(content="How should I brew it?"))
        trimmed = trim_messages_to_window(messages,window_size=2)
        print("会话配置:", config)
        for message in trimmed:
            print(message.type, ":", message.content)
        logger.info("原消息%d条，裁剪后%d条；已创建MemorySaver，尚未接入业务图", len(messages), len(trimmed))
        return 0
    except Exception:
        logger.exception("记忆模块演示失败")
        return 1

if __name__ == '__main__':
    raise SystemExit(main())