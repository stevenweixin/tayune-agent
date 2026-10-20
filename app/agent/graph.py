"""Agent 图的组装入口。

State -> 模型节点 -> 工具节点 -> 模型，带有限反馈循环。
复用LangGraph而非自己写无限while。这里只读工具，每次用户请求最多调用模型4次；
# 最多接受4个工具请求尝试，未知工具和无效参数也消耗预算。。
"""
import asyncio
import json
from langchain_core.messages import AIMessage,ToolMessage
from langgraph.graph import StateGraph,START,END
from pydantic import ValidationError
from app.agent.state import TeaState

FALLBACK =  "I couldn't verify enough information right now. Please try again later or use the shop's contact page."


def build_graph(model, tools):
    allowed = {}
    for item in tools:
        allowed[item.name] = item

    async def decide(state):
        # 查次数,已调用模型 4 次,不许再调,直接用兜底回答并标记降级。
        if state.get("model_count", 0) >= 4:
            return {"answer": FALLBACK, "degraded": True}

        # 调用模型,接住失败,转成兜底回答
        try:
            response = await model.ask(state["messages"])
        except (ValueError, RuntimeError):
            return {"answer": FALLBACK, "degraded": True}

        # 把模型的回复追加进消息列表,次数 +1,token 累加
        usage = response.usage_metadata or {}
        update = {"messages": state["messages"] + [response],
                  "model_count": state.get("model_count", 0) + 1,
                  "input_tokens": state.get("input_tokens", 0) + usage.get("input_tokens", 0),
                  "output_tokens": state.get("output_tokens", 0) + usage.get("output_tokens", 0)}
        # 如果模型没有请求工具,说明这就是最终回答,写进 answer。
        if not response.tool_calls:
            update["answer"] = response.content.strip()
        return update

    async def execute_tools(state):
        messages = list(state["messages"])
        response = messages[-1]
        count = state.get("tool_count", 0)
        known = list(state.get("known_product_ids", []))
        sources = list(state.get("sources", []))
        trace = list(state.get("trace", []))
        for call in response.tool_calls:
            name, args = call["name"], call["args"]
            result = {"ok": False, "error": "tool_not_allowed"}
            if count >= 4:
                result = {"ok": False, "error": "tool_budget_exhausted"}
            else:
                count += 1
                if name in allowed:
                    if name == "get_product" and args.get("product_id") not in known:
                        result = {"ok": False, "error": "search_for_product_id_first"}
                    else:
                        try:
                            result = await asyncio.wait_for(allowed[name].ainvoke(args), timeout=40)
                        except ValidationError:
                            result = {"ok": False, "error": "invalid_arguments"}
                        except Exception:
                            result = {"ok": False, "error": "tool_unavailable"}
            trace.append(name + (":ok" if result.get("ok") else ":failed"))
            if result.get("ok"):
                if name == "search_products":
                    for product in result["data"]["products"]:
                        if product["id"] not in known:
                            known.append(product["id"])
                        sources.append({"title": product["name"], "url": product["url"]})
                elif name == "get_product":
                    product = result["data"]
                    sources.append({"title": product["name"], "url": product["url"]})
                elif name == "search_knowledge":
                    for passage in result["passages"]:
                        if passage["url"]:
                            sources.append({"title": passage["title"], "url": passage["url"]})
            # 必须对应原tool_call_id，每一个请求都回传结果，保持协议完整。
            messages.append(ToolMessage(content=json.dumps(result, ensure_ascii=False),
                                        tool_call_id=call["id"], name=name))
        return {"messages": messages, "tool_count": count, "known_product_ids": known[-100:],
                "sources": sources, "trace": trace}

    def route(state):
        if state.get("answer"):
            return END
        return "tools" if state["messages"][-1].tool_calls else END

    builder = StateGraph(TeaState)
    builder.add_node("model", decide)
    builder.add_node("tools", execute_tools)
    builder.add_edge(START, "model")
    builder.add_conditional_edges("model", route)
    builder.add_edge("tools", "model")
    # 本图每次请求新建State；会话服务保存精简Human/AI历史，不保存所有工具中间步骤。
    return builder.compile()
