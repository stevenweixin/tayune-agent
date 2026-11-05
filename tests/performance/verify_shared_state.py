"""新建专属测试PostgreSQL并执行共享状态契约；结束移除本次容器，保留私有原始记录。"""
import argparse
import asyncio
import hashlib
import importlib.metadata
import io
import json
import os
import secrets
import socket
import subprocess
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deploy/state/compose.test.yaml"
PROJECT = "tayune-state-capacity-verify"
CONTAINER = "tayune-capacity-postgres-test"


class ContractResults(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rows = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.rows.append({"case": test.id(), "pass": True})

    def addFailure(self, test, error):
        super().addFailure(test, error)
        self.rows.append({"case": test.id(), "pass": False, "type": "failure"})

    def addError(self, test, error):
        super().addError(test, error)
        self.rows.append({"case": test.id(), "pass": False, "type": "error"})


def verify(output):
    if output.exists() or not output.is_relative_to(ROOT / "reports"):
        raise ValueError("Output must be a new reports/ directory")
    # 在任何启动操作之前，确认专属容器名和端口未被占用，绝不接管其他容器。
    existing = subprocess.check_output(["docker", "ps", "-aq", "--filter", "name=^/" + CONTAINER + "$"], text=True).strip()
    if existing:
        raise RuntimeError("Dedicated test container already exists; refusing to replace it")
    with socket.socket() as probe:
        # 检查实际监听者；bind会把刚停止连接的TIME_WAIT误判成仍有服务。
        probe.settimeout(.3)
        if probe.connect_ex(("127.0.0.1", 25432)) == 0:
            raise RuntimeError("Dedicated test port is already listening")
    output.mkdir(parents=True)
    data = output / "pgdata"
    data.mkdir()
    password = secrets.token_urlsafe(24)
    env = dict(os.environ, TAYUNE_STATE_TEST_PASSWORD=password, TAYUNE_STATE_TEST_DATA=str(data))
    dsn = "postgresql://tayune_capacity_test:" + password + "@127.0.0.1:25432/tayune_capacity_test"
    manifest = {"commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "driver": importlib.metadata.version("asyncpg"), "database": "dedicated loopback25432",
        "paid_model_calls": 0, "production_changed": False, "formal_api_connected": False,
        "source_sha256": {}}
    sources = ["app/database/shared_state.py", "app/database/shared_schema.sql",
               "tests/integration/test_shared_state.py", "tests/performance/shared_state_worker.py",
               "tests/performance/verify_shared_state.py", "deploy/state/compose.test.yaml"]
    for name in sources:
        manifest["source_sha256"][name] = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2))
    command = ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE)]
    started = time.monotonic()
    old_dsn = os.environ.get("TAYUNE_STATE_TEST_DSN")
    result = None
    try:
        with (output / "container.log").open("x") as log:
            subprocess.run(command + ["up", "-d", "--pull", "never"], env=env, stdout=log, stderr=log, check=True)
            for _ in range(40):
                ready = subprocess.run(["docker", "exec", CONTAINER, "pg_isready", "-h", "127.0.0.1", "-U", "tayune_capacity_test",
                                        "-d", "tayune_capacity_test"], stdout=log, stderr=log)
                if ready.returncode == 0:
                    break
                time.sleep(.5)
            else:
                raise RuntimeError("Test PostgreSQL did not become ready")
        # initdb临时Unix服务不代表宿主机TCP已就绪；用真实驱动再确认一次。
        async def tcp_ready():
            import asyncpg
            for _ in range(30):
                try:
                    connection = await asyncpg.connect(dsn, timeout=1)
                    try:
                        return await connection.fetchval("SHOW server_version")
                    finally:
                        await connection.close()
                except (OSError, ConnectionError, TimeoutError, asyncpg.PostgresError):
                    await asyncio.sleep(.2)
            raise RuntimeError("Dedicated PostgreSQL TCP connection did not become ready")
        manifest["postgres_version"] = asyncio.run(tcp_ready())
        manifest["postgres_image_id"] = subprocess.check_output(["docker", "inspect", "--format", "{{.Image}}", CONTAINER], text=True).strip()
        os.environ["TAYUNE_STATE_TEST_DSN"] = dsn
        # 数据库只在此受保护的专属DSN下才允许被测试清理。
        suite = unittest.defaultTestLoader.loadTestsFromName("tests.integration.test_shared_state")
        stream = io.StringIO()
        result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=ContractResults).run(suite)
        log_path = output / "contracts.log"
        log_path.write_text(stream.getvalue(), encoding="utf-8")
        log_path.chmod(0o600)
        summary = {"checks": result.rows, "passed": sum(row["pass"] for row in result.rows),
                   "total": result.testsRun, "skipped": len(result.skipped),
                   "paid_model_calls": 0, "formal_api_connected": False,
                   "elapsed_seconds": time.monotonic() - started}
        (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2))
        print(json.dumps({key: value for key, value in summary.items() if key != "checks"}), flush=True)
        if not result.wasSuccessful() or result.skipped:
            raise RuntimeError("Shared-state contracts failed; see private contracts.log")
    finally:
        if old_dsn is None:
            os.environ.pop("TAYUNE_STATE_TEST_DSN", None)
        else:
            os.environ["TAYUNE_STATE_TEST_DSN"] = old_dsn
        # 仅清理本次拥有的专属compose项目；不带-v，原始数据库留在reports/。
        with (output / "shutdown.log").open("x") as log:
            subprocess.run(command + ["down"], env=env, stdout=log, stderr=log, check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    verify(args.output.resolve())


if __name__ == "__main__":
    main()
