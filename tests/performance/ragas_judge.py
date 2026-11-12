"""评审模型物理HTTP尝试记账：覆盖SDK/结构化解析层重试，未知用量即停止。"""
import fcntl
import hashlib
import json
import os
import time
from contextvars import ContextVar
from pathlib import Path

import httpx

ACTIVE_SCORE = ContextVar("active_ragas_score", default={})
INPUT_RATE = .30
OUTPUT_RATE = 1.20


class JudgeBudgetStop(RuntimeError):
    pass


class JudgeBudget:
    def __init__(self, path, *, max_attempts=250, max_usd=.50):
        self.path = Path(path)
        self.max_attempts, self.max_usd = max_attempts, max_usd
        if type(max_attempts) is not int or not 1 <= max_attempts <= 250 or not 0 < max_usd <= .50:
            raise ValueError("Judge authorization ceiling is250 attempts/USD0.50")
        self.file = self.path.open("a+", encoding="utf-8")
        os.chmod(self.path, 0o600)
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("Another judge process owns this authorization ledger") from None
        self.file.seek(0)
        records = [json.loads(line) for line in self.file if line.strip()]
        reserved = {row["attempt"] for row in records if row["event"] == "reserved"}
        finished = {row["attempt"] for row in records if row["event"] == "finished"}
        self.attempts = max(reserved, default=0)
        self.estimated_usd = sum(row.get("estimated_usd", 0) for row in records if row["event"] == "finished")
        self.inflight = {}
        self.stopped = None
        if reserved != finished or any(row.get("usage_known") is False for row in records):
            self.stopped = "unknown_provider_usage"
        if self.attempts >= max_attempts or self.estimated_usd >= max_usd:
            self.stopped = self.stopped or "authorization_limit"

    def append(self, row):
        self.file.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        self.file.flush()
        os.fsync(self.file.fileno())

    def reserve(self, payload_bytes):
        if self.stopped:
            raise JudgeBudgetStop(self.stopped)
        payload = json.loads(payload_bytes)
        if payload.get("model") != "deepseek-flash" or payload.get("stream", False):
            raise ValueError("Only non-streaming official flash judge calls are allowed")
        maximum_output = payload.get("max_tokens", payload.get("max_completion_tokens"))
        if type(maximum_output) is not int or not 1 <= maximum_output <= 2048 or len(payload_bytes) > 100000:
            raise ValueError("Unbounded judge input/output")
        # 用UTF8字节数加协议余量估算输入上界；不是把字符数当精确Token数。
        upper_input = len(payload_bytes) + 1024
        upper_cost = (upper_input * INPUT_RATE + maximum_output * OUTPUT_RATE) / 1_000_000
        if (self.attempts >= self.max_attempts
                or self.estimated_usd + sum(self.inflight.values()) + upper_cost > self.max_usd):
            self.stopped = "authorization_limit"
            raise JudgeBudgetStop(self.stopped)
        self.attempts += 1
        self.inflight[self.attempts] = upper_cost
        row = {"event": "reserved", "attempt": self.attempts, **ACTIVE_SCORE.get(),
               "request_hash": hashlib.sha256(payload_bytes).hexdigest(),
               "input_upper_tokens": upper_input, "output_upper_tokens": maximum_output,
               "upper_estimated_usd": upper_cost}
        self.append(row)
        return row

    def finish(self, reserved, body=None, error=None, elapsed_seconds=0):
        self.inflight.pop(reserved["attempt"])
        row = {"event": "finished", "attempt": reserved["attempt"], **ACTIVE_SCORE.get(),
               "elapsed_seconds": elapsed_seconds}
        usage = body.get("usage") if isinstance(body, dict) else None
        if (not isinstance(usage, dict) or type(usage.get("prompt_tokens")) is not int
                or type(usage.get("completion_tokens")) is not int
                or min(usage["prompt_tokens"], usage["completion_tokens"]) < 0):
            self.stopped = "unknown_provider_usage"
            row.update(usage_known=False, error_type=type(error).__name__ if error else "missing_usage")
        else:
            estimate = (usage["prompt_tokens"] * INPUT_RATE + usage["completion_tokens"] * OUTPUT_RATE) / 1_000_000
            self.estimated_usd += estimate
            row.update(usage_known=True, usage=usage, estimated_usd=estimate, actual_model=body.get("model"))
            if estimate > reserved["upper_estimated_usd"]:
                self.stopped = "estimate_bound_exceeded"
        self.append(row)

    def summary(self):
        return {"physical_attempts": self.attempts, "conservative_estimated_usd": self.estimated_usd,
                "max_attempts": self.max_attempts, "max_estimated_usd": self.max_usd, "stopped": self.stopped}

    def close(self):
        self.file.close()


class JudgeTransport(httpx.AsyncBaseTransport):
    def __init__(self, budget, raw_output, inner=None):
        self.budget = budget
        self.raw_output = Path(raw_output)
        self.inner = inner if inner is not None else httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request):
        if (request.url.scheme != "https" or request.url.host != "api.deepseek.com"
                or request.url.path not in ("/chat/completions", "/v1/chat/completions")):
            raise ValueError("Judge destination is not the approved official endpoint")
        reserved = self.budget.reserve(await request.aread())
        started = time.perf_counter()
        body = None
        try:
            response = await self.inner.handle_async_request(request)
            body = json.loads(await response.aread())
        except BaseException as error:
            self.budget.finish(reserved, error=error, elapsed_seconds=time.perf_counter() - started)
            raise
        self.budget.finish(reserved, body, elapsed_seconds=time.perf_counter() - started)
        # 只写评审输出和Token用量，不保存Authorization头或密钥。
        with self.raw_output.open("a", encoding="utf-8") as file:
            os.chmod(self.raw_output, 0o600)
            file.write(json.dumps({"attempt": reserved["attempt"], **ACTIVE_SCORE.get(),
                                   "status": response.status_code, "response": body}, ensure_ascii=False) + "\n")
        return response

    async def aclose(self):
        await self.inner.aclose()
