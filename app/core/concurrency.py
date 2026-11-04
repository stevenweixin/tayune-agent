"""单进程准入：有限执行、有限短等待、同会话互斥；不是分布式锁。"""
import asyncio
import math
import os
from collections import deque
from contextlib import asynccontextmanager, contextmanager
from time import perf_counter


def read_chat_capacity():
    # 这是本机保护上限，不是已验证的吞吐；调大后仍须重新压测。
    value = int(os.getenv("TAYUNE_MAX_CONCURRENT_CHATS", "4"))
    if not 1 <= value <= 32:
        raise ValueError("TAYUNE_MAX_CONCURRENT_CHATS must be between 1 and 32")
    return value


def read_capacity_settings():
    settings = {"max_concurrent_chats": read_chat_capacity(),
                "max_waiting_chats": int(os.getenv("TAYUNE_MAX_WAITING_CHATS", "16")),
                "admission_wait_seconds": float(os.getenv("TAYUNE_ADMISSION_WAIT_SECONDS", "2")),
                "max_sessions": int(os.getenv("TAYUNE_MAX_SESSIONS", "5000"))}
    if not 0 <= settings["max_waiting_chats"] <= 128:
        raise ValueError("TAYUNE_MAX_WAITING_CHATS must be between0 and128")
    timeout = settings["admission_wait_seconds"]
    if not math.isfinite(timeout) or not .01 <= timeout <= 5:
        raise ValueError("TAYUNE_ADMISSION_WAIT_SECONDS must be between0.01 and5")
    if not 1 <= settings["max_sessions"] <= 100000:
        raise ValueError("TAYUNE_MAX_SESSIONS must be between1 and100000")
    return settings


class RequestCoordinator:
    def __init__(self, capacity=4, max_waiting=0, wait_seconds=2):
        if type(capacity) is not int or not 1 <= capacity <= 32:
            raise ValueError("Invalid chat capacity")
        self.capacity = capacity
        self.active = 0
        self.peak = 0
        self.owners = set()
        if type(max_waiting) is not int or not 0 <= max_waiting <= 128:
            raise ValueError("Invalid admission queue size")
        if not math.isfinite(wait_seconds) or not .01 <= wait_seconds <= 5:
            raise ValueError("Invalid admission wait timeout")
        self.max_waiting = max_waiting
        self.wait_seconds = wait_seconds
        self.waiters = deque()
        self.peak_waiting = 0
        self.rejected = 0
        self.timed_out = 0
        self.wait_ms_total = 0.0

    @contextmanager
    def session(self, token):
        # 检查和登记之间没有await；同一事件循环中不会同时占用同一个会话。
        if token in self.owners:
            raise RuntimeError("session_busy")
        self.owners.add(token)
        try:
            yield
        finally:
            self.owners.remove(token)

    @contextmanager
    def slot(self):
        # 兼容显式立即拒绝的调用者；不能插队抢走已交给等待者的名额。
        if self.active >= self.capacity or self.waiters:
            raise RuntimeError("busy")
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            yield
        finally:
            self._release()

    def _release(self):
        while self.waiters:
            waiter = self.waiters.popleft()
            if not waiter.done():
                # 直接移交当前名额，active不先减再加，防止新请求抢占。
                waiter.set_result(None)
                return
        self.active -= 1

    async def _acquire(self):
        if self.active < self.capacity and not self.waiters:
            self.active += 1
            self.peak = max(self.peak, self.active)
            return
        if self.max_waiting == 0:
            self.rejected += 1
            raise RuntimeError("busy")
        if len(self.waiters) >= self.max_waiting:
            self.rejected += 1
            raise RuntimeError("admission_queue_full")
        waiter = asyncio.get_running_loop().create_future()
        self.waiters.append(waiter)
        self.peak_waiting = max(self.peak_waiting, len(self.waiters))
        started = perf_counter()
        try:
            # shield使我们能区分：还在排队被取消，还是刚拿到名额便被取消。
            # timeout上下文保留外部取消，避免名额刚移交时wait_for吞掉取消的竞争。
            async with asyncio.timeout(self.wait_seconds):
                await asyncio.shield(waiter)
        except BaseException as error:
            if waiter.done() and not waiter.cancelled():
                self._release()  # 名额已移交，但调用者还没开始执行，必须归还。
            else:
                waiter.cancel()
                self.waiters.remove(waiter)
            if isinstance(error, TimeoutError):
                self.timed_out += 1
                raise RuntimeError("admission_timeout") from None
            raise
        finally:
            self.wait_ms_total += (perf_counter() - started) * 1000

    @asynccontextmanager
    async def admit(self):
        await self._acquire()
        try:
            yield
        finally:
            self._release()

    def snapshot(self):
        return {"capacity": self.capacity, "active": self.active, "peak_active": self.peak,
                "waiting": len(self.waiters), "max_waiting": self.max_waiting,
                "peak_waiting": self.peak_waiting, "owners": len(self.owners),
                "rejected": self.rejected, "timed_out": self.timed_out,
                "wait_ms_total": self.wait_ms_total}
