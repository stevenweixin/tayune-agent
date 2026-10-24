"""三个模型角色共用预算、错误码、重试和熔断，不叠加 SDK 重试。"""
import asyncio
from copy import deepcopy
from time import monotonic, perf_counter

import httpx
from langchain_core.messages import SystemMessage

from app.core.execution import current_execution, fingerprint


class ModelUnavailable(RuntimeError):
    pass


def classify_error(error):
    status = getattr(error, "status_code", None)
    if status in (401, 403):
        return "model_auth", False
    if status in (408, 429, 500, 502, 503, 504):
        return "model_transient", True
    if isinstance(error, (TimeoutError, httpx.TimeoutException, httpx.NetworkError)):
        return "model_transport", True
    # OpenAI 的网络错误有自己的异常类型；不使用原异常正文分类或记录。
    if type(error).__name__ in ("APIConnectionError", "APITimeoutError"):
        return "model_transport", True
    return "model_rejected", False


class ModelGateway:
    def __init__(self, routes, names, fallback=None, input_rate=None, output_rate=None):
        self.routes = routes
        self.names = names
        self.fallback = fallback
        self.input_rate = input_rate
        self.output_rate = output_rate
        self.failures = {}
        self.open_until = {}

    def role(self, role):
        return RoleModel(self, role)

    async def invoke(self, role, messages, tools=None, options=None):
        execution = current_execution()
        if monotonic() < self.open_until.get(role, 0):
            raise ModelUnavailable("model_circuit_open")
        for attempt in range(2):
            if execution:
                execution.reserve_model()
            model = self.routes[role]
            name = self.names[role]
            if attempt and self.fallback:
                model, name = self.fallback
            if tools is not None:
                model = model.bind_tools(tools)
            call_messages = messages
            call_options = dict(options or {})
            # 按物理次数判断，包含复核/摘要/重试；最后一次不能再提出无法收尾的新工具请求。
            final_only = (execution is not None and role == "main" and tools is not None
                          and (execution.model_calls >= 4 or execution.tool_attempts >= 4))
            if final_only:
                call_options["tool_choice"] = "none"
                call_messages = list(messages) + [SystemMessage(content=(
                    "This is the final answer opportunity within this request's budget. "
                    "No more tool calls are available. Select relevant current-turn evidence units "
                    "using the required JSON contract, or use an allowed fixed reply code if "
                    "evidence is insufficient. Do not invent missing details."))]
                from app.integrations.deepseek_tools import check_messages
                check_messages(call_messages)
                execution.event("model_finalization", reason="model_budget" if execution.model_calls >= 4 else "tool_budget")
            if call_options:
                model = model.bind(**call_options)
            started = perf_counter()
            try:
                response = await asyncio.wait_for(model.ainvoke(call_messages), timeout=25)
            except asyncio.CancelledError:
                if execution:
                    execution.usage_complete = False
                    execution.event("model_result", role=role, model=name, error="cancelled_or_deadline")
                raise
            except Exception as error:
                code, retryable = classify_error(error)
                if execution:
                    execution.usage_complete = False
                    execution.event("model_result", role=role, model=name, error=code,
                                    retryable=retryable, elapsed=round(perf_counter() - started, 3))
                if not retryable:
                    raise ModelUnavailable(code) from None
                self.failures[role] = self.failures.get(role, 0) + 1
                if self.failures[role] >= 3:
                    self.open_until[role] = monotonic() + 30
                if attempt == 1 or self.failures[role] >= 3:
                    raise ModelUnavailable(code) from None
                await asyncio.sleep(0.2)
                continue
            self.failures[role] = 0
            usage = response.usage_metadata or {}
            if execution:
                execution.record_usage(usage)
                estimate = None
                if self.input_rate is not None and self.output_rate is not None:
                    estimate = (usage.get("input_tokens", 0) * self.input_rate
                                + usage.get("output_tokens", 0) * self.output_rate) / 1_000_000
                execution.event("model_result", role=role, model=name, attempt=attempt + 1,
                                prompt_hash=fingerprint(messages[0].content if messages else ""),
                                input_tokens=usage.get("input_tokens", 0), output_tokens=usage.get("output_tokens", 0),
                                estimated_usd=estimate, elapsed=round(perf_counter() - started, 3))
            if final_only and response.tool_calls:
                # 供应商若忽略none，也不能执行这批请求或放宽总预算。
                execution.event("model_finalization_rejected", reason="unexpected_tool_calls")
                raise ModelUnavailable("model_ignored_finalization")
            return response

    async def summarize(self, messages):
        response = await self.invoke("summary", messages, options={"max_tokens": 256})
        if (response.response_metadata.get("finish_reason") != "stop" or response.tool_calls
                or not isinstance(response.content, str) or not response.content.strip()):
            raise ModelUnavailable("invalid_summary")
        return {"text": response.content.strip(), "usage": response.usage_metadata}


class RoleModel:
    """适配现有 ToolModel / IntentReviewer 的 ainvoke、bind、bind_tools 接口。"""
    def __init__(self, gateway, role, tools=None, options=None):
        self.gateway = gateway
        self.role = role
        self.tools = tools
        self.options = options or {}

    def bind(self, **options):
        return RoleModel(self.gateway, self.role, self.tools, {**self.options, **options})

    def bind_tools(self, tools):
        return RoleModel(self.gateway, self.role, tools, deepcopy(self.options))

    async def ainvoke(self, messages):
        return await self.gateway.invoke(self.role, messages, self.tools, self.options)
