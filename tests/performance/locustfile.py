"""真实loopback HTTP压力，依赖由测试服务模拟；只允许8012，禁止公网目标。"""
import json
import os
import time
import uuid
from pathlib import Path

from locust import HttpUser, between, events, task
from locust.exception import StopUser

ROWS = []


@events.test_start.add_listener
def validate_target(environment, **kwargs):
    if environment.host != "http://127.0.0.1:8012":
        raise RuntimeError("This load test only allows the isolated loopback server on 8012")


@events.test_stop.add_listener
def save_results(environment, **kwargs):
    output = Path(os.environ["TAYUNE_LOAD_OUTPUT"]).resolve()
    with output.open("x", encoding="utf-8") as file:
        for row in ROWS:
            file.write(json.dumps(row) + "\n")


class TeaUser(HttpUser):
    wait_time = between(0.2, 0.4)

    def on_start(self):
        if self.host != "http://127.0.0.1:8012":
            raise StopUser()
        # 每个用户轮换四个测试会话；仍保留正式Service的8秒规则，拒绝单独计数。
        self.tokens = []
        self.index = 0
        for _ in range(4):
            response = self.client.post("/api/session", json={})
            if response.status_code != 200:
                raise StopUser()
            self.tokens.append(response.json()["session_token"])

    @task
    def ask(self):
        token = self.tokens[self.index % len(self.tokens)]
        self.index += 1
        started = time.perf_counter()
        with self.client.post("/api/chat", name="chat/json", catch_response=True,
                              headers={"Authorization": "Bearer " + token},
                              json={"message": "Show one oolong under 30 USD.", "request_id": uuid.uuid4().hex}) as response:
            row = {"elapsed_ms": (time.perf_counter() - started) * 1000,
                   "status": response.status_code, "business_success": False}
            if response.status_code == 200:
                try:
                    body = response.json()
                    row["business_success"] = (body.get("degraded") is False
                                               and body.get("task_status") == "completed"
                                               and "USD 20.00" in body.get("answer", ""))
                except (ValueError, TypeError):
                    pass
            if not row["business_success"]:
                # 429仍属于未完成业务；不把快速拒绝伪装成高吞吐成功。
                response.failure("rejected_429" if response.status_code == 429 else "business_not_completed")
            ROWS.append(row)
