"""仅用于已授权评测的跨角色总预算；不改变生产网关。"""
import json
import time
from app.core.execution import current_execution


class EvaluationBudget:
    def __init__(self, output, max_attempts=120, max_usd=1.0):
        self.output = output
        self.max_attempts = max_attempts
        self.max_usd = max_usd
        self.attempts = 0
        self.estimated_usd = 0.0
        self.stopped = None
        self.records = []

    def reserve(self):
        if self.stopped:
            raise RuntimeError("evaluation_budget_stopped")
        if self.attempts >= self.max_attempts or self.estimated_usd >= self.max_usd - .05:
            self.stopped = "attempt_or_cost_limit"
            raise RuntimeError("evaluation_budget_exhausted")
        # 单次输入有项目字符上限，输出最多512；额外预留5美分，不等超限才停止。
        self.attempts += 1
        return self.attempts

    def finish(self, number, role, started, response=None, error=None):
        row = {"attempt": number, "role": role, "elapsed_seconds": time.perf_counter() - started}
        execution = current_execution()
        if execution:
            row["run_id"] = execution.run_id
        usage = response.usage_metadata if response is not None else None
        if not usage or type(usage.get("input_tokens")) is not int or type(usage.get("output_tokens")) is not int:
            self.stopped = "unknown_provider_usage"
            row.update(usage_known=False, error_type=type(error).__name__ if error else "missing_usage")
        else:
            # 按官方高峰、输入不命中缓存价格估算，真实账单可能更低；不是账户扣款记录。
            estimate = (usage["input_tokens"] * .30 + usage["output_tokens"] * 1.20) / 1_000_000
            self.estimated_usd += estimate
            row.update(usage_known=True, usage=usage, estimated_usd=estimate,
                       actual_model=response.response_metadata.get("model_name"),
                       finish_reason=response.response_metadata.get("finish_reason"))
        self.records.append(row)
        with self.output.open("a", encoding="utf-8") as file:
            file.write(json.dumps(row) + "\n")


class BudgetModel:
    """每一次ainvoke对应SDK的一次物理尝试；SDK重试必须为0。"""
    def __init__(self, model, budget, role):
        self.model, self.budget, self.role = model, budget, role

    def bind(self, **options):
        return BudgetModel(self.model.bind(**options), self.budget, self.role)

    def bind_tools(self, tools):
        return BudgetModel(self.model.bind_tools(tools), self.budget, self.role)

    async def ainvoke(self, messages):
        number = self.budget.reserve()
        started = time.perf_counter()
        try:
            response = await self.model.ainvoke(messages)
        except BaseException as error:
            # 包括超时取消：供应商可能已经计费，未知用量后不继续新尝试。
            self.budget.finish(number, self.role, started, error=error)
            raise
        self.budget.finish(number, self.role, started, response=response)
        return response
