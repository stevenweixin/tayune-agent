"""把模型提出的工具请求转成可验证的步骤，而不是执行自由文本计划。"""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

PUBLIC_TOOLS = {"search_products", "get_product", "search_knowledge"}


class StepSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    tool: str
    args: dict
    depends_on: list[str] = Field(default_factory=list)
    risk: Literal["public_read"] = "public_read"


class WorkflowPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str = Field(min_length=1, max_length=500)
    constraints: dict = Field(default_factory=dict)
    steps: list[StepSpec] = Field(max_length=4)

    def validate_business(self, allowed=PUBLIC_TOOLS):
        pending = {}
        for step in self.steps:
            if step.id in pending or step.tool not in allowed:
                raise ValueError("duplicate_step_or_unknown_tool")
            pending[step.id] = set(step.depends_on)
        all_ids = set(pending)
        for dependencies in pending.values():
            if not dependencies.issubset(all_ids):
                raise ValueError("missing_dependency")
        done = set()
        while pending:
            ready = []
            for step_id, dependencies in pending.items():
                if dependencies.issubset(done):
                    ready.append(step_id)
            if not ready:
                raise ValueError("cyclic_dependencies")
            for step_id in ready:
                done.add(step_id)
                del pending[step_id]
        return self
