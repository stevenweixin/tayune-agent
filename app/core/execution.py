"""单请求执行上下文：预算、版本、审计和可恢复检查点。"""
import hashlib
import json
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import perf_counter

from langchain_core.messages import messages_from_dict, messages_to_dict

CURRENT_RUN = ContextVar("tayune_run", default=None)


def fingerprint(value) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def encode_state(state):
    saved = dict(state)
    saved["messages"] = messages_to_dict(state.get("messages", []))
    return saved


def decode_state(saved):
    state = dict(saved)
    state["messages"] = messages_from_dict(saved.get("messages", []))
    return state


@dataclass
class Execution:
    run_id: str
    store: object
    versions: dict
    model_calls: int = 0
    tool_attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usage_complete: bool = True
    started: float = field(default_factory=perf_counter)

    def event(self, kind, **details):
        # 调用方只传固定错误码、数量、哈希和来源编号，不传对话、密钥或工具全文。
        self.store.event(self.run_id, kind, details)

    def reserve_model(self):
        if self.model_calls >= 4 or self.input_tokens + self.output_tokens >= 32000:
            raise RuntimeError("model_budget_exhausted")
        self.model_calls += 1
        self.store.counters(self.run_id, self.counters())
        self.event("model_attempt", number=self.model_calls)

    def record_usage(self, usage):
        if not usage:
            self.usage_complete = False
            return
        self.input_tokens += usage.get("input_tokens", 0)
        self.output_tokens += usage.get("output_tokens", 0)
        self.store.counters(self.run_id, self.counters())

    def checkpoint(self, state, next_node):
        self.store.checkpoint(self.run_id, encode_state(state), next_node, self.counters())

    def counters(self):
        return {"model_calls": self.model_calls, "tool_attempts": self.tool_attempts,
                "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "usage_complete": self.usage_complete}

    def usage(self):
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens}


def current_execution():
    return CURRENT_RUN.get()
