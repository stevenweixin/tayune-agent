"""独立发压进程：每虚拟用户一个预置会话，逐行落盘，不使用生产令牌。"""
import json
import os
import random
import time
import uuid
from pathlib import Path

import gevent
from gevent.event import Event
from locust import HttpUser, between, events, task
from locust.exception import StopUser

TOKENS = []
GATE = Event()
READY = 0
PEAK = 0
COUNT = 0
STARTED = None
STREAM = None
MONITOR = None
SPIKE = os.getenv("TAYUNE_SCALE_SPIKE") == "1"
TARGET = int(os.getenv("TAYUNE_SCALE_USERS", "1"))


@events.test_start.add_listener
def start(environment, **kwargs):
    global TOKENS, STARTED, STREAM, MONITOR
    if environment.host != "http://127.0.0.1:8012":
        raise RuntimeError("Only isolated loopback8012 is allowed")
    TOKENS = json.loads(Path(os.environ["TAYUNE_SCALE_TOKENS"]).read_text())[:TARGET]
    if len(TOKENS) < TARGET:
        raise RuntimeError("Insufficient synthetic sessions")
    STARTED = time.monotonic()
    STREAM = Path(os.environ["TAYUNE_LOAD_OUTPUT"]).open("x", encoding="utf-8")

    def monitor():
        global PEAK
        while True:
            PEAK = max(PEAK, environment.runner.user_count)
            if SPIKE and READY == TARGET:
                GATE.set()
            gevent.sleep(.05)
    MONITOR = gevent.spawn(monitor)


@events.test_stop.add_listener
def stop(environment, **kwargs):
    if MONITOR:
        MONITOR.kill()
    if STREAM:
        STREAM.close()
    Path(os.environ["TAYUNE_LOAD_OUTPUT"] + ".meta.json").write_text(json.dumps(
        {"peak_virtual_users": PEAK, "ready_users": READY, "requests": COUNT,
         "duration_seconds": time.monotonic() - STARTED, "spike_gate_released": GATE.is_set()}))


class TeaUser(HttpUser):
    # 阅读/思考时间；不是每0.2秒疯狂重复发送请求。
    wait_time = between(20, 40)

    def on_start(self):
        global READY
        if not TOKENS:
            raise StopUser()
        self.token = TOKENS.pop()
        self.index = READY
        READY += 1
        self.sent = False

    @task
    def ask(self):
        global COUNT
        if SPIKE:
            GATE.wait(timeout=20)
            if not GATE.is_set():
                raise StopUser()
            if self.sent:
                gevent.sleep(3600)
                return
        self.sent = True
        question = "Show one oolong under 30 USD." if self.index % 2 == 0 else "What is your return policy?"
        started = time.monotonic()
        # 每次新request_id，防止把幂等缓存重放计为新问答吞吐。
        with self.client.post("/api/chat", name="chat/json", timeout=30, catch_response=True,
                              headers={"Authorization": "Bearer " + self.token},
                              json={"message": question, "request_id": uuid.uuid4().hex}) as response:
            body = {}
            if response.status_code == 200:
                try:
                    body = response.json()
                except ValueError:
                    pass
            answer = body.get("answer", "")
            expected = "USD 20.00" in answer if self.index % 2 == 0 else "Opened tea cannot be returned" in answer
            success = response.status_code == 200 and body.get("degraded") is False and expected
            if not success:
                response.failure("rejected_429" if response.status_code == 429 else "business_not_completed")
            row = {"user_index": self.index, "started_offset_seconds": started - STARTED,
                   "elapsed_ms": (time.monotonic() - started) * 1000, "status": response.status_code,
                   "business_success": success, "degraded": body.get("degraded"),
                   "error_type": type(response.error).__name__ if response.error else None}
            STREAM.write(json.dumps(row) + "\n")
            COUNT += 1
            if COUNT >= 20000:
                self.environment.runner.quit()
