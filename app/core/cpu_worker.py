"""同步CPU推理在线程中执行，超时后线程仍可能运行，不能立刻再排入工作。"""
import asyncio
import logging
from time import perf_counter
from app.core.execution import current_execution

logger = logging.getLogger("tayune_agent.cpu")


class CPUWorker:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.tasks = set()
        self.busy = False

    async def run(self, function, *args):
        if self.busy:
            raise RuntimeError("CPU模型正忙")
        self.busy = True

        async def execute():
            async with self.lock:
                return await asyncio.to_thread(function, *args)

        task = asyncio.create_task(execute())
        self.tasks.add(task)
        # 保留任务直到完成，shield防止请求超时取消await后错误地释放推理锁。
        def completed(done):
            self.busy = False
            self.tasks.discard(done)
            if not done.cancelled():
                done.exception()  # 取走异常，避免后台任务产生未读取异常警告。
        task.add_done_callback(completed)
        return await asyncio.shield(task)

    async def close(self):
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)


class CPUQueue:
    """分类和RAG共享的有界队列，仍由CPUWorker保证一次只有一个推理线程。"""
    def __init__(self, max_pending=16, timeout=35):
        if type(max_pending) is not int or max_pending < 1 or timeout <= 0:
            raise ValueError("Invalid CPU queue limits")
        self.worker = CPUWorker()
        self.serial = asyncio.Lock()
        self.max_pending = max_pending
        self.timeout = timeout
        self.pending = 0
        self.peak_pending = 0
        self.closed = False

    @property
    def busy(self):
        return self.worker.busy

    async def run(self, function, *args):
        if self.closed:
            raise RuntimeError("cpu_queue_closed")
        if self.pending >= self.max_pending:
            raise RuntimeError("cpu_queue_full")
        self.pending += 1
        self.peak_pending = max(self.peak_pending, self.pending)
        submitted = perf_counter()
        execution = current_execution()
        run_id = execution.run_id if execution else "startup-or-health"

        async def execute():
            async with self.serial:
                # 等待者超时/取消不会停止原线程；必须等残留线程真正结束再派发。
                if self.worker.tasks:
                    await asyncio.shield(asyncio.gather(*self.worker.tasks, return_exceptions=True))
                if self.closed:
                    raise RuntimeError("cpu_queue_closed")
                entered = perf_counter()
                try:
                    return await self.worker.run(function, *args)
                finally:
                    # 只记录阶段和耗时，不保存参数、问题或无限增长的样本列表。
                    logger.info("stage=cpu request=%s operation=%s queue_ms=%.1f run_ms=%.1f", run_id,
                                getattr(function, "__name__", "operation"),
                                (entered - submitted) * 1000, (perf_counter() - entered) * 1000)
        try:
            # 等待和执行共享期限；CPUWorker的shield仍保护实际推理线程。
            return await asyncio.wait_for(execute(), timeout=self.timeout)
        finally:
            self.pending -= 1

    async def close(self):
        self.closed = True
        await self.worker.close()

