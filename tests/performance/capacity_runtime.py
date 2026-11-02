"""只供8012容量实验注入的准入/CPU调度；不替换生产Service和CPUWorker。"""
import asyncio
import time
from collections import defaultdict

from app.core.cpu_worker import CPUWorker
from tests.performance.server import ObservedService


class CapacityGate:
    """兼容Service的锁接口，无等待队列，超过执行名额立即拒绝。"""
    def __init__(self, capacity):
        if capacity not in (1, 2, 4, 8):
            raise ValueError("Only bounded experimental capacities are allowed")
        self.capacity = capacity
        self.active = 0
        self.peak = 0

    def locked(self):
        return self.active >= self.capacity

    async def __aenter__(self):
        # 单事件循环中检查和加一之间没有await，不会抢占同一个名额。
        if self.locked():
            raise RuntimeError("busy")
        self.active += 1
        self.peak = max(self.peak, self.active)
        return self

    async def __aexit__(self, *args):
        self.active -= 1


class CapacityService(ObservedService):
    def __init__(self, runtime, capacity):
        super().__init__(runtime, daily_limit=1000)
        self.lock = CapacityGate(capacity)
        self.owners = set()

    async def chat(self, token, *args, **kwargs):
        # 同一会话仍只允许一条在途问答，防止历史丢失；原幂等/8秒/预算逻辑照常运行。
        if token in self.owners:
            self.outcomes["session_busy"] += 1
            raise RuntimeError("session_busy")
        self.owners.add(token)
        try:
            return await super().chat(token, *args, **kwargs)
        finally:
            self.owners.discard(token)


class QueuedCPU:
    """有限队列包装原CPUWorker，分类器与RAG共用；只有实验进程使用。"""
    def __init__(self, max_pending=16, timeout=35):
        self.worker = CPUWorker()
        self.serial = asyncio.Lock()
        self.max_pending = max_pending
        self.timeout = timeout
        self.pending = 0
        self.peak_pending = 0
        self.timings = defaultdict(list)

    @property
    def busy(self):
        return self.worker.busy

    async def run(self, function, *args):
        if self.pending >= self.max_pending:
            raise RuntimeError("cpu_queue_full")
        self.pending += 1
        self.peak_pending = max(self.peak_pending, self.pending)
        submitted = time.perf_counter()
        stage = getattr(function, "__name__", "operation")

        async def execute():
            async with self.serial:
                # 上一个调用者即使超时，shield保护的CPU线程也可能未结束。
                if self.worker.tasks:
                    await asyncio.shield(asyncio.gather(*self.worker.tasks, return_exceptions=True))
                entered = time.perf_counter()
                try:
                    return await self.worker.run(function, *args)
                finally:
                    self.timings[stage].append({"queue_ms": (entered - submitted) * 1000,
                                               "run_ms": (time.perf_counter() - entered) * 1000})
        try:
            return await asyncio.wait_for(execute(), timeout=self.timeout)
        finally:
            self.pending -= 1

    async def close(self):
        await self.worker.close()
