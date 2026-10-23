"""Agent 图的组装入口。

State -> 模型节点 -> 工具节点 -> 模型，带有限反馈循环。
复用LangGraph而非自己写无限while。这里只读工具，每次用户请求最多调用模型4次；
# 最多接受4个工具请求尝试，未知工具和无效参数也消耗预算。。
"""
import asyncio
import json
import logging
from pathlib import Path
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langgraph.graph import StateGraph, START, END
from app.agent.state import TeaState
from app.agent.answer_guard import FALLBACK, REPAIR_PROMPT, check_answer
from app.core.logger import setup_logging
from app.core.execution import current_execution
from app.agent.plan import StepSpec, WorkflowPlan
from app.agent.tool_gateway import ToolGateway

logger = logging.getLogger("tayune_agent.graph")


def register_source(sources: list[dict], title: str, url: str | None,
                    evidence_key: str | None = None) -> str | None:
    """有网页按URL归并；无网页按真实片段ID归并。两种来源都能领取本轮编号。"""
    key = url or evidence_key
    if not key:
        return None
    for source in sources:
        if (source.get("evidence_key") or source.get("url")) == key:
            return source["source_id"]
    source_id = "S" + str(len(sources) + 1)
    sources.append({"source_id": source_id, "title": title, "url": url, "evidence_key": key})
    execution = current_execution()
    if execution:
        from app.core.execution import fingerprint
        execution.event("evidence", source_id=source_id, evidence_hash=fingerprint(key), has_public_url=bool(url))
    return source_id


def build_graph(model, tools):
    # 复用现有轮换/脱敏配置，在处理第一条请求前启用日志。
    # setup_logging 会复用已有 handler；若入口已配置别的路径，以首次配置为准。
    setup_logging(Path(__file__).resolve().parents[2] / "logs/agent.log")
    allowed = {}
    for item in tools:
        allowed[item.name] = item
    gateway = ToolGateway(tools)

    async def decide(state):
        if state.get("model_count", 0) >= 4:
            return {"answer": FALLBACK, "degraded": True, "sources": []}
        try:
            response = await model.ask(state["messages"])
        except (ValueError, RuntimeError):
            return {"answer": FALLBACK, "degraded": True, "sources": []}
        usage = response.usage_metadata or {}
        update = {"messages": state["messages"] + [response],
                  "model_count": state.get("model_count", 0) + 1,
                  "input_tokens": state.get("input_tokens", 0) + usage.get("input_tokens", 0),
                  "output_tokens": state.get("output_tokens", 0) + usage.get("output_tokens", 0)}
        if not response.tool_calls:
            try:
                result = check_answer(response.content, state.get("sources", []))
            except ValueError as error:
                # error来自自己的固定错误码，不记录草稿、用户问题或工具正文。
                logger.warning("stage=answer_guard status=rejected reason=%s", error)
                trace = list(state.get("trace", []))
                repair_count = state.get("answer_repair_count", 0)
                if repair_count >= 1 or update["model_count"] >= 4:
                    update.update(answer=FALLBACK, sources=[], degraded=True)
                    trace.append("answer_guard:fallback")
                else:
                    # 丢弃未通过的草稿，避免模型照抄刚才编出的配方。
                    # 这是程序自己的系统反馈，不拼接用户提供的文本或错误详情。
                    update["messages"] = state["messages"] + [SystemMessage(content=REPAIR_PROMPT)]
                    update["answer_repair_count"] = repair_count + 1
                    trace.append("answer_guard:repair")
                update["trace"] = trace
            else:
                # 保持前端接口不变：返回普通英文和链接，不把内部控制代码显示出去。
                update.update(result)
                update["trace"] = list(state.get("trace", [])) + ["answer_guard:accepted"]
        return update

    async def execute_tools(state):
        messages = list(state["messages"])
        response = messages[-1]
        count = state.get("tool_count", 0)
        known = list(state.get("known_product_ids", []))
        sources = list(state.get("sources", []))
        trace = list(state.get("trace", []))
        execution = current_execution()
        # 每轮由原生tool_calls生成小计划，不为简单寒暄额外调用一次Planner模型。
        # 同轮读取互不依赖；详情必须使用前一轮搜索已核验的ID，不能猜测未来的结果。
        steps = []
        for call in response.tool_calls[:max(0, 4 - count)]:
            if call["name"] in allowed:
                steps.append(StepSpec(id=call["id"], tool=call["name"], args=call["args"]))
        plan = WorkflowPlan(goal=state.get("goal") or "Answer the public tea-shop request using verified evidence",
                            constraints=state.get("constraints", {}), steps=steps)
        plan.validate_business()
        if execution:
            execution.event("plan", steps=[{"id": step.id, "tool": step.tool, "depends_on": step.depends_on} for step in steps])

        async def run_call(call, permitted):
            if not permitted:
                return {"ok": False, "data": None, "error": "tool_budget_exhausted"}
            return await gateway.invoke(call, known)

        # 最多两路工具读取并发，CPUWorker仍串行；结果按原请求顺序归并，保持来源编号稳定。
        if len(steps) == len(response.tool_calls):
            completed = await gateway.execute_plan(plan, list(known))
            results = [completed[call["id"]] for call in response.tool_calls]
        else:
            jobs = []
            for index, call in enumerate(response.tool_calls):
                jobs.append(run_call(call, count + index < 4))
            results = await asyncio.gather(*jobs)
        for call, result in zip(response.tool_calls, results):
            name, args = call["name"], call["args"]
            log_name = name if name in allowed else "unknown_tool"
            count = min(count + 1, 4)
            logger.info("stage=tool status=%s name=%s error=%s used=%d",
                        "ok" if result.get("ok") else "failed", log_name,
                        result.get("error", "none"), count)
            trace.append(log_name + (":ok" if result.get("ok") else ":failed"))
            if result.get("ok"):
                if name == "search_products":
                    for product in result["data"]["products"]:
                        if product["id"] not in known:
                            known.append(product["id"])
                        product["source_id"] = register_source(sources, product["name"], product["url"])
                elif name == "get_product":
                    product = result["data"]
                    product["source_id"] = register_source(sources, product["name"], product["url"])
                elif name == "search_knowledge":
                    for passage in result["passages"]:
                        passage["source_id"] = register_source(
                            sources, passage["title"], passage["url"], passage.get("evidence_key"))
            # 必须对应原tool_call_id，每一个请求都回传结果，保持协议完整。
            messages.append(ToolMessage(content=json.dumps(result, ensure_ascii=False),
                                        tool_call_id=call["id"], name=name))
        return {"messages": messages, "tool_count": count, "known_product_ids": known[-100:],
                "sources": sources, "trace": trace, "plan": plan.model_dump()}

    def route(state):
        if state.get("answer"):
            return END
        last_message = state["messages"][-1]
        if isinstance(last_message, AIMessage) and last_message.tool_calls:
            return "tools"
        # 没有最终回答，也没有工具调用：门禁要求纠正，回模型节点而不是提前END。
        # repair_count和model_count共同限制循环，原service的12步上限仍足够。
        return "model"

    async def model_node(state):
        update = await decide(state)
        execution = current_execution()
        if execution:
            merged = {**state, **update}
            execution.event("model_decision", next_node=route(merged),
                            tool_requests=len(getattr(merged["messages"][-1], "tool_calls", [])),
                            degraded=merged.get("degraded", False))
            execution.checkpoint(merged, route(merged))
        return update

    async def tools_node(state):
        update = await execute_tools(state)
        execution = current_execution()
        if execution:
            execution.checkpoint({**state, **update}, "model")
        return update

    builder = StateGraph(TeaState)
    builder.add_node("model", model_node)
    builder.add_node("tools", tools_node)
    builder.add_conditional_edges(START, lambda state: state.get("resume_node") or "model")
    builder.add_conditional_edges("model", route)
    builder.add_edge("tools", "model")
    # SQLite执行检查点与会话历史分离；恢复入口只由后端创建，客户端不能提交State。
    return builder.compile()
