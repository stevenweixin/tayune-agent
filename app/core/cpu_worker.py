"""同步CPU推理在线程中执行，超时后线程仍可能运行，不能立刻再排入工作。"""
import asyncio


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

