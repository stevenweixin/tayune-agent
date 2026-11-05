"""共享状态独立进程契约；无HTTP、模型或Milvus请求，仅连接专属测试库。"""
import asyncio
import json
import os
import sys
from urllib.parse import urlparse

from app.database.shared_state import SharedState, SharedStateError


def require_test_dsn():
    dsn = os.environ.get("TAYUNE_STATE_TEST_DSN", "")
    parsed = urlparse(dsn)
    # 测试会清理自己的表；严格拒绝课堂5433端口、默认库或远程地址。
    if (parsed.scheme != "postgresql" or parsed.hostname != "127.0.0.1"
            or parsed.port != 25432 or parsed.path != "/tayune_capacity_test"):
        raise ValueError("Use the dedicated loopback25432/tayune_capacity_test database only")
    return dsn


async def worker():
    # 凭据、会话Token通过环境/标准输入传递，不放命令行参数或stdout。
    payload = json.loads(await asyncio.to_thread(sys.stdin.readline))
    store = await SharedState.connect(require_test_dsn(), capacity=4, max_sessions=5000, lease_seconds=2)
    lease = None
    try:
        print(json.dumps({"state": "ready", "pid": os.getpid()}), flush=True)
        await asyncio.to_thread(sys.stdin.readline)
        try:
            lease = await store.acquire_session(payload["token"])
            started = await store.start_run(lease, payload["key"], "fixture tea question", {"code": "fixture"}, daily_limit=100)
        except SharedStateError as error:
            print(json.dumps({"state": "rejected", "code": str(error)}), flush=True)
            return
        run_id = started["run"]["id"]
        await store.checkpoint(lease, run_id, {"evidence_started_at": started["session"]["data"]["last_request"]},
                               "model", {"model_calls": 1})
        await store.record_step(lease, run_id, "tool-1", "fixture-payload", "running")
        print(json.dumps({"state": "started", "run_id": run_id, "fence": lease.fence}), flush=True)
        # 父进程确认全局名额后才提交。崩溃场景会在这里终止进程。
        await asyncio.to_thread(sys.stdin.readline)
        await store.renew(lease)
        data = started["session"]["data"]
        data["history"] = [{"role": "human", "text": "fixture tea question"}, {"role": "ai", "text": "fixture answer"}]
        await store.finish(lease, run_id, "completed", {"answer": "fixture answer"}, {"model_calls": 1},
                           data=data, revision=started["session"]["revision"])
        print(json.dumps({"state": "completed", "run_id": run_id}), flush=True)
    finally:
        if lease:
            await store.release(lease)
        await store.close()


if __name__ == "__main__":
    asyncio.run(worker())
