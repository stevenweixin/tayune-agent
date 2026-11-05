"""共享底座入口参数与测试库隔离检查；不要求安装驱动或启动数据库。"""
import unittest
from unittest.mock import patch

from app.database.shared_state import SharedState, digest
from tests.performance.shared_state_worker import require_test_dsn


class SharedStateValidation(unittest.IsolatedAsyncioTestCase):
    async def test_capacity_is_bounded_before_importing_driver(self):
        for value in (0, 129, True, 2.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                await SharedState.connect("unused", capacity=value)

    async def test_sessions_are_bounded_before_importing_driver(self):
        for value in (0, 100001, True, 2.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                await SharedState.connect("unused", max_sessions=value)

    async def test_lease_must_have_finite_short_duration(self):
        for value in (.01, 61, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                await SharedState.connect("unused", lease_seconds=value)

    async def test_test_runner_refuses_course_remote_or_default_database(self):
        for dsn in ("", "postgresql://u:p@127.0.0.1:5433/tayune_capacity_test",
                    "postgresql://u:p@127.0.0.1:25432/postgres",
                    "postgresql://u:p@example.com:25432/tayune_capacity_test"):
            with self.subTest(dsn=dsn), patch.dict("os.environ", {"TAYUNE_STATE_TEST_DSN": dsn}):
                with self.assertRaises(ValueError):
                    require_test_dsn()

    async def test_session_digest_is_stable_and_not_plaintext(self):
        self.assertEqual(digest("fixture"), digest("fixture"))
        self.assertEqual(len(digest("fixture")), 64)
        self.assertNotEqual(digest("fixture"), digest("other fixture"))
