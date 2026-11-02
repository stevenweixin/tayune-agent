"""单进程准入：不同会话有限并发，同会话写入互斥，不建立无限等待队列。"""
import os
from contextlib import contextmanager


def read_chat_capacity():
    # 这是本机保护上限，不是已验证的吞吐；调大后仍须重新压测。
    value = int(os.getenv("TAYUNE_MAX_CONCURRENT_CHATS", "4"))
    if not 1 <= value <= 32:
        raise ValueError("TAYUNE_MAX_CONCURRENT_CHATS must be between 1 and 32")
    return value


class RequestCoordinator:
    def __init__(self, capacity=4):
        if type(capacity) is not int or not 1 <= capacity <= 32:
            raise ValueError("Invalid chat capacity")
        self.capacity = capacity
        self.active = 0
        self.peak = 0
        self.owners = set()

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
        if self.active >= self.capacity:
            raise RuntimeError("busy")
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            yield
        finally:
            self.active -= 1
