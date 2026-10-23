"""只读工具入口：注册、参数校验、权限、有限并行、步骤结果恢复。"""
import asyncio
from copy import deepcopy
from dataclasses import dataclass
from time import perf_counter
from urllib.parse import urlparse

from pydantic import ValidationError

from app.core.execution import current_execution, fingerprint

AUDITED_ARGUMENTS = {"search", "product_scope", "page", "per_page", "max_price_usd", "product_id", "query", "source_type"}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    version: str = "1"
    permission: str = "public_read"
    timeout: float = 40
    # 读操作允许显式恢复；本轮不自动重试工具，避免CPU超时后重复堆积推理。
    max_attempts: int = 1


def normalize_result(result):
    if not isinstance(result, dict) or type(result.get("ok")) is not bool:
        return {"ok": False, "data": None, "error": "invalid_tool_result"}
    result = deepcopy(result)
    if not result["ok"]:
        # 不把外部 message、异常正文反射给模型或审计。
        allowed_errors = {"invalid_arguments", "tool_timeout", "tool_budget_exhausted", "tool_not_allowed",
                          "search_for_product_id_first", "product_search_unavailable", "product_detail_unavailable",
                          "knowledge_unavailable", "tool_unavailable"}
        code = result.get("error")
        return {"ok": False, "data": None, "error": code if code in allowed_errors else "tool_unavailable"}
    if "passages" in result:
        result["data"] = {"passages": result["passages"], "top_score": result.get("top_score")}
    if not isinstance(result.get("data"), dict):
        return {"ok": False, "data": None, "error": "invalid_tool_result"}
    result["error"] = None
    return result


def validate_success(name, result):
    if not result["ok"]:
        return result
    if name == "search_products":
        items = result["data"].get("products")
    elif name == "get_product":
        items = [result["data"]]
    else:
        items = result["data"].get("passages")
    if not isinstance(items, list) or len(items) > 20:
        return {"ok": False, "data": None, "error": "invalid_tool_result"}
    for item in items:
        if not isinstance(item, dict):
            return {"ok": False, "data": None, "error": "invalid_tool_result"}
        title = item.get("title") if name == "search_knowledge" else item.get("name")
        url = item.get("url")
        if not isinstance(title, str) or not title.strip():
            return {"ok": False, "data": None, "error": "invalid_tool_result"}
        if url is not None:
            parsed = urlparse(url) if isinstance(url, str) else None
            if (parsed is None or parsed.scheme != "https" or parsed.netloc != "tayunetea.com"
                    or parsed.username or parsed.password):
                return {"ok": False, "data": None, "error": "invalid_tool_result"}
        if name != "search_knowledge" and (type(item.get("id")) is not int or item["id"] <= 0 or not url):
            return {"ok": False, "data": None, "error": "invalid_tool_result"}
    return result


class ToolGateway:
    def __init__(self, tools):
        self.tools = {}
        self.specs = {}
        for tool in tools:
            if tool.name not in ("search_products", "get_product", "search_knowledge") or tool.name in self.tools:
                raise ValueError("invalid_tool_registration")
            self.tools[tool.name] = tool
            self.specs[tool.name] = ToolSpec(tool.name)
        self.parallel = asyncio.Semaphore(2)

    async def execute_plan(self, plan, known_ids):
        plan.validate_business(set(self.tools))
        pending = {step.id: step for step in plan.steps}
        completed = {}
        while pending:
            ready = []
            for step in pending.values():
                if all(dependency in completed for dependency in step.depends_on):
                    ready.append(step)
            jobs = []
            for step in ready:
                failed_dependency = any(not completed[item]["ok"] for item in step.depends_on)
                if failed_dependency:
                    completed[step.id] = {"ok": False, "data": None, "error": "dependency_failed"}
                    execution = current_execution()
                    if execution:
                        execution.store.save_step(execution.run_id, step.id,
                            fingerprint({"tool": step.tool, "args": step.args}), "skipped", completed[step.id])
                        execution.event("step_skipped", step_id=step.id, reason="dependency_failed")
                else:
                    jobs.append((step.id, self.invoke({"id": step.id, "name": step.tool, "args": step.args}, known_ids)))
            results = await asyncio.gather(*(item[1] for item in jobs))
            for (step_id, _), result in zip(jobs, results):
                completed[step_id] = result
                if result["ok"] and pending[step_id].tool == "search_products":
                    for product in result["data"]["products"]:
                        if product["id"] not in known_ids:
                            known_ids.append(product["id"])
            for step in ready:
                del pending[step.id]
        return completed

    async def invoke(self, call, known_ids):
        name, args = call["name"], call["args"]
        execution = current_execution()
        safe_name = name if name in self.tools else "unknown_tool"
        payload_hash = fingerprint({"tool": name, "args": args})
        step_id = call["id"]
        if execution:
            cached = execution.store.step(execution.run_id, step_id, payload_hash)
            if cached is not None:
                execution.event("tool_recovered", step_id=step_id, tool=safe_name)
                return deepcopy(cached)
            # 每个请求尝试都占预算，包括未知名称、无效参数；已完成步骤恢复不重复扣。
            if execution.tool_attempts >= 4:
                return {"ok": False, "data": None, "error": "tool_budget_exhausted"}
            execution.tool_attempts += 1
            execution.store.counters(execution.run_id, execution.counters())
            execution.store.save_step(execution.run_id, step_id, payload_hash, "running")
        started = perf_counter()
        result = None
        authorized = name in self.tools
        if not authorized:
            result = {"ok": False, "error": "tool_not_allowed"}
        elif name == "get_product" and args.get("product_id") not in known_ids:
            authorized = False
            result = {"ok": False, "error": "search_for_product_id_first"}
        if execution:
            argument_hashes = {}
            for key, value in args.items():
                if key in AUDITED_ARGUMENTS:
                    argument_hashes[key] = fingerprint(value)
            execution.event("tool_call", step_id=step_id, tool=safe_name, authorized=authorized,
                            args_hash=payload_hash, arg_keys=[key for key in sorted(args) if key in AUDITED_ARGUMENTS],
                            argument_hashes=argument_hashes, version="1", risk="public_read")
        if result is None:
            tool = self.tools[name]
            try:
                schema = getattr(tool, "args_schema", None)
                if schema is not None:
                    schema.model_validate(args)
                cached = execution.store.recent_result(execution.run_id, payload_hash) if execution else None
                if cached is not None:
                    result = cached
                    execution.event("tool_cache_hit", step_id=step_id, tool=safe_name)
                else:
                    async with self.parallel:
                        if execution:
                            execution.event("tool_execute", step_id=step_id, tool=safe_name)
                        result = await asyncio.wait_for(tool.ainvoke(args), timeout=self.specs[name].timeout)
            except ValidationError:
                result = {"ok": False, "error": "invalid_arguments"}
            except TimeoutError:
                result = {"ok": False, "error": "tool_timeout"}
            except asyncio.CancelledError:
                if execution:
                    execution.store.save_step(execution.run_id, step_id, payload_hash, "unknown")
                    execution.event("tool_result", step_id=step_id, tool=safe_name, error="cancelled_work_may_continue")
                raise
            except Exception:
                result = {"ok": False, "error": "tool_unavailable"}
        result = validate_success(name, normalize_result(result))
        if execution:
            status = "succeeded" if result["ok"] else "failed"
            if result.get("error") == "tool_timeout":
                status = "timed_out"
            execution.store.save_step(execution.run_id, step_id, payload_hash, status, result)
            execution.event("tool_result", step_id=step_id, tool=safe_name, status=status,
                            error=result.get("error"), result_hash=fingerprint(result),
                            elapsed=round(perf_counter() - started, 3))
        return result
