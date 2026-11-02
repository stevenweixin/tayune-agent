"""固定混合负载，只能向隔离8012发请求；不使用真实供应商做洪泛。"""
import json
import os
import time
import uuid
from pathlib import Path

from locust import HttpUser, between, events, task
from locust.exception import StopUser

ROWS = []


@events.test_stop.add_listener
def save(environment, **kwargs):
    with Path(os.environ["TAYUNE_LOAD_OUTPUT"]).open("x") as file:
        for row in ROWS:
            file.write(json.dumps(row) + "\n")


class CapacityUser(HttpUser):
    wait_time = between(.2, .4)

    def on_start(self):
        if self.host != "http://127.0.0.1:8012":
            raise StopUser()
        self.tokens = []
        self.index = 0
        for _ in range(4):
            response = self.client.post("/api/session")
            if response.status_code != 200:
                raise StopUser()
            self.tokens.append(response.json()["session_token"])

    @task
    def ask(self):
        token = self.tokens[self.index % 4]
        question = "Do you sell jasmine tea?" if self.index % 2 == 0 else "What is your return policy?"
        kind = "product" if self.index % 2 == 0 else "policy"
        self.index += 1
        started = time.perf_counter()
        with self.client.post("/api/chat", name="mixed/chat", catch_response=True,
            headers={"Authorization": "Bearer " + token},
            json={"message": question, "request_id": uuid.uuid4().hex}) as response:
            row = {"kind": kind, "status": response.status_code,
                   "elapsed_ms": (time.perf_counter() - started) * 1000, "business_success": False}
            if response.status_code == 200:
                body = response.json()
                marker = "USD 20.00" if kind == "product" else "Opened tea cannot be returned"
                row["business_success"] = body.get("degraded") is False and marker in body.get("answer", "")
                row["degraded"] = body.get("degraded")
            if not row["business_success"]:
                response.failure("rejected_or_degraded")
            ROWS.append(row)
