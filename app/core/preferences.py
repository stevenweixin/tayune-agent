"""只接收顾客明确提交的偏好；不把模型推测和商品实时状态写成长期事实。"""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class PreferencePatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    budget_usd: float | None = Field(default=None, gt=0, le=10000)
    taste: Literal["light", "strong", "floral", "roasted", "sweet"] | None = None
    vessel: Literal["mug", "gaiwan", "teapot", "infuser"] | None = None
    tea_type: Literal["black", "green", "white", "oolong", "puer", "herbal"] | None = None


def update_preferences(existing, patch):
    values = dict(existing)
    for field, value in patch.model_dump(exclude_unset=True).items():
        if value is None:
            values.pop(field, None)
        else:
            values[field] = {"value": value, "source": "user_confirmed"}
    return values
