"""容量阶段二：异步共享状态底座，尚未接替正式 SQLite Runtime。

短事务只用于领取/保存状态，不在数据库事务中等待模型或工具。
租约代表临时执行权；fence 是递增的执行代号，防止旧实例复活后覆盖新实例。
本模块不保证外部 API 恰好执行一次；所有写接口要求当前有效租约。
"""
import hashlib
import json
import math
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path


class SharedStateError(RuntimeError):
    """固定错误码，不把数据库地址、凭据或原始 SQL 异常交给访客。"""


@dataclass(frozen=True)
class SessionLease:
    session_id: str
    holder: str
    fence: int


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class SharedState:
    def __init__(self, pool, capacity, max_sessions, lease_seconds):
        self.pool = pool
        self.capacity = capacity
        self.max_sessions = max_sessions
        self.lease_seconds = lease_seconds

    @classmethod
    async def connect(cls, dsn, *, capacity=4, max_sessions=5000, lease_seconds=15):
        if type(capacity) is not int or not 1 <= capacity <= 128:
            raise ValueError("invalid_shared_capacity")
        if type(max_sessions) is not int or not 1 <= max_sessions <= 100000:
            raise ValueError("invalid_shared_session_capacity")
        if not math.isfinite(lease_seconds) or not .1 <= lease_seconds <= 60:
            raise ValueError("invalid_lease_duration")
        # 可选依赖只在显式启用本模块时导入，不影响现有正式环境。
        import asyncpg

        async def initialize_connection(connection):
            await connection.set_type_codec("jsonb", schema="pg_catalog",
                                            encoder=json.dumps, decoder=json.loads)

        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4, timeout=3,
            command_timeout=3, init=initialize_connection,
            server_settings={"application_name": "tayune-shared-state", "lock_timeout": "500ms"})
        store = cls(pool, capacity, max_sessions, lease_seconds)
        try:
            await store.initialize()
        except BaseException:
            await pool.close()
            raise
        return store

    @asynccontextmanager
    async def transaction(self):
        # 有限连接池与获取期限，不让数据库故障形成无限等待。
        async with self.pool.acquire(timeout=1) as connection:
            async with connection.transaction():
                yield connection

    async def initialize(self):
        schema = Path(__file__).with_name("shared_schema.sql").read_text(encoding="utf-8")
        async with self.transaction() as connection:
            # 只串行化建表/配置；不是把所有聊天串行化的全局锁。
            await connection.execute("SELECT pg_advisory_xact_lock(79110405)")
            await connection.execute(schema)
            await connection.execute("""INSERT INTO ta_settings VALUES(TRUE,$1,$2,$3)
                ON CONFLICT DO NOTHING""", self.capacity, self.max_sessions, self.lease_seconds)
            settings = await connection.fetchrow("SELECT * FROM ta_settings")
            if (settings["capacity"], settings["max_sessions"], settings["lease_seconds"]) != (
                    self.capacity, self.max_sessions, self.lease_seconds):
                raise SharedStateError("shared_settings_mismatch")
            await connection.execute("""INSERT INTO ta_slots(id) SELECT generate_series(1,$1::int)
                ON CONFLICT DO NOTHING""", self.capacity)

    async def close(self):
        await self.pool.close()

    async def create_session(self):
        token = secrets.token_urlsafe(32)
        data = {"summary": "", "history": [], "known_product_ids": [],
                "last_request": 0, "preferences": {}}
        async with self.transaction() as connection:
            # 注册容量检查需要串行的短事务，否则多个实例可能同时越过上限。
            await connection.fetchrow("SELECT singleton FROM ta_settings FOR UPDATE")
            count = await connection.fetchval("SELECT count(*) FROM ta_sessions WHERE expires_at>clock_timestamp()")
            if count >= self.max_sessions:
                raise SharedStateError("session_capacity")
            await connection.execute("""INSERT INTO ta_sessions(id,data,expires_at)
                VALUES($1,$2,clock_timestamp()+interval '24 hours')""", digest(token), data)
        return token

    async def get_session(self, token):
        async with self.pool.acquire(timeout=1) as connection:
            row = await connection.fetchrow("""SELECT data,revision,expires_at FROM ta_sessions
                WHERE id=$1 AND expires_at>clock_timestamp()""", digest(token))
            return dict(row) if row else None

    async def acquire_session(self, token):
        holder = secrets.token_hex(16)
        async with self.transaction() as connection:
            row = await connection.fetchrow("""UPDATE ta_sessions SET holder=$2,
                fence=nextval('ta_fence'), lease_until=clock_timestamp()+$3::interval
                WHERE id=$1 AND expires_at>clock_timestamp()
                  AND (holder IS NULL OR lease_until<=clock_timestamp())
                RETURNING id,fence""", digest(token), holder, timedelta(seconds=self.lease_seconds))
            if row is None:
                exists = await connection.fetchval("""SELECT 1 FROM ta_sessions
                    WHERE id=$1 AND expires_at>clock_timestamp()""", digest(token))
                raise SharedStateError("session_busy" if exists else "session_expired")
            lease = SessionLease(row["id"], holder, row["fence"])
            # 只有取得该会话的新执行权后才恢复旧任务，不误伤其他实例的活任务。
            await connection.execute("""UPDATE ta_steps SET status='unknown' WHERE status='running'
                AND run_id IN (SELECT id FROM ta_runs WHERE session_id=$1 AND status='running')""", lease.session_id)
            await connection.execute("""UPDATE ta_runs SET status='interrupted',updated_at=clock_timestamp()
                WHERE session_id=$1 AND status='running'""", lease.session_id)
        return lease

    async def guard_session(self, connection, lease):
        # 行锁覆盖检查和写入；仅比较一个内存布尔值无法防止租约已被另一实例接管。
        row = await connection.fetchrow("""SELECT data,revision,lease_until FROM ta_sessions
            WHERE id=$1 AND holder=$2 AND fence=$3 AND lease_until>clock_timestamp()
              AND expires_at>clock_timestamp() FOR UPDATE""", lease.session_id, lease.holder, lease.fence)
        if row is None:
            raise SharedStateError("lease_lost")
        return row

    async def renew(self, lease):
        async with self.transaction() as connection:
            await self.guard_session(connection, lease)
            running = await connection.fetchval("""SELECT EXISTS(SELECT 1 FROM ta_runs
                WHERE session_id=$1 AND fence=$2 AND status='running')""", lease.session_id, lease.fence)
            deadline = await connection.fetchval("SELECT clock_timestamp()+$1::interval", timedelta(seconds=self.lease_seconds))
            slot = await connection.fetchval("""UPDATE ta_slots SET lease_until=$4
                WHERE session_id=$1 AND holder=$2 AND fence=$3 AND lease_until>clock_timestamp()
                RETURNING id""", lease.session_id, lease.holder, lease.fence, deadline)
            if running and slot is None:
                # 会话检查后若名额过期/已转交，不能只续会话而让执行越过全局容量。
                raise SharedStateError("execution_slot_lost")
            await connection.execute("UPDATE ta_sessions SET lease_until=$2 WHERE id=$1", lease.session_id, deadline)
            cancelled = await connection.fetchval("""SELECT EXISTS(SELECT 1 FROM ta_runs
                WHERE session_id=$1 AND fence=$2 AND status='running' AND cancel_requested)""",
                lease.session_id, lease.fence)
            return {"lease_until": deadline, "cancel_requested": cancelled}

    async def release(self, lease):
        async with self.transaction() as connection:
            # 必须带holder和fence，旧持有者不能释放新持有者的执行权。
            row = await connection.fetchrow("""UPDATE ta_sessions SET holder=NULL,lease_until=NULL
                WHERE id=$1 AND holder=$2 AND fence=$3 RETURNING id""",
                lease.session_id, lease.holder, lease.fence)
            if row is None:
                return False
            await self.free_slot(connection, lease)
            return True

    async def free_slot(self, connection, lease):
        await connection.execute("""UPDATE ta_slots SET holder=NULL,fence=NULL,session_id=NULL,lease_until=NULL
            WHERE session_id=$1 AND holder=$2 AND fence=$3""", lease.session_id, lease.holder, lease.fence)

    async def claim_slot(self, connection, lease, deadline):
        slot = await connection.fetchval("""SELECT id FROM ta_slots
            WHERE holder IS NULL OR lease_until<=clock_timestamp()
            ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED""")
        if slot is None:
            raise SharedStateError("capacity_full")
        await connection.execute("""UPDATE ta_slots SET session_id=$2,holder=$3,fence=$4,lease_until=$5
            WHERE id=$1""", slot, lease.session_id, lease.holder, lease.fence, deadline)

    async def start_run(self, lease, request_key, question, versions, *, daily_limit=20):
        if not isinstance(request_key, str) or not 16 <= len(request_key) <= 100:
            raise ValueError("invalid_request_key")
        if type(daily_limit) is not int or daily_limit < 1:
            raise ValueError("invalid_daily_limit")
        async with self.transaction() as connection:
            session = await self.guard_session(connection, lease)
            previous = await connection.fetchrow("SELECT * FROM ta_runs WHERE session_id=$1 AND request_key=$2",
                                                 lease.session_id, request_key)
            if previous:
                if previous["payload_hash"] != digest(question):
                    raise SharedStateError("idempotency_conflict")
                # 只交付短时、同版本结果。恢复旧任务必须使用专门的恢复接口。
                fresh = await connection.fetchval("SELECT $1::timestamptz>clock_timestamp()-interval '60 seconds'",
                                                   previous["created_at"])
                evidence_time = (previous["checkpoint"] or {}).get("evidence_started_at")
                if evidence_time is not None:
                    fresh = fresh and await connection.fetchval("""SELECT to_timestamp($1::double precision)
                        >clock_timestamp()-interval '60 seconds'""", evidence_time)
                if previous["result"] is None or previous["status"] not in ("completed", "degraded"):
                    raise SharedStateError("task_requires_recovery")
                if previous["versions"] != versions or not fresh:
                    raise SharedStateError("stale_request_result")
                return {"created": False, "run": dict(previous), "session": dict(session)}
            another = await connection.fetchval("""SELECT 1 FROM ta_runs WHERE session_id=$1
                AND status='running'""", lease.session_id)
            if another:
                raise SharedStateError("session_busy")
            now = await connection.fetchval("SELECT clock_timestamp()")
            data = dict(session["data"])
            if now.timestamp() - data["last_request"] < 8:
                raise SharedStateError("rate_limit")
            await self.claim_slot(connection, lease, session["lease_until"])
            day = await connection.fetchval("SELECT (clock_timestamp() AT TIME ZONE 'UTC')::date")
            await connection.execute("INSERT INTO ta_budget VALUES($1,0) ON CONFLICT DO NOTHING", day)
            count = await connection.fetchval("""UPDATE ta_budget SET count=count+1
                WHERE day=$1 AND count<$2 RETURNING count""", day, daily_limit)
            if count is None:
                raise SharedStateError("daily_limit")
            run_id = secrets.token_hex(16)
            row = await connection.fetchrow("""INSERT INTO ta_runs
                (id,session_id,request_key,payload_hash,fence,status,versions)
                VALUES($1,$2,$3,$4,$5,'running',$6) RETURNING *""",
                run_id, lease.session_id, request_key, digest(question), lease.fence, versions)
            data["last_request"] = now.timestamp()
            saved = await self._write_session(connection, lease, data, session["revision"])
            return {"created": True, "run": dict(row), "session": dict(saved)}

    async def _write_session(self, connection, lease, data, revision):
        row = await connection.fetchrow("""UPDATE ta_sessions SET data=$2,revision=revision+1,
            expires_at=clock_timestamp()+interval '24 hours' WHERE id=$1 AND revision=$3
            RETURNING data,revision,expires_at""", lease.session_id, data, revision)
        if row is None:
            raise SharedStateError("session_revision_conflict")
        return row

    async def save_session(self, lease, data, *, revision):
        async with self.transaction() as connection:
            await self.guard_session(connection, lease)
            return dict(await self._write_session(connection, lease, data, revision))

    async def guard_run(self, connection, lease, run_id):
        session = await self.guard_session(connection, lease)
        row = await connection.fetchrow("""SELECT * FROM ta_runs
            WHERE id=$1 AND session_id=$2 AND fence=$3 AND status='running' FOR UPDATE""",
            run_id, lease.session_id, lease.fence)
        if row is None:
            raise SharedStateError("run_not_owned")
        slot = await connection.fetchval("""SELECT id FROM ta_slots WHERE session_id=$1 AND holder=$2
            AND fence=$3 AND lease_until>clock_timestamp() FOR UPDATE""", lease.session_id, lease.holder, lease.fence)
        if slot is None:
            raise SharedStateError("execution_slot_lost")
        return session, row

    async def checkpoint(self, lease, run_id, state, next_node, counters):
        async with self.transaction() as connection:
            await self.guard_run(connection, lease, run_id)
            await connection.execute("""UPDATE ta_runs SET checkpoint=$2,next_node=$3,counters=$4,
                updated_at=clock_timestamp() WHERE id=$1""", run_id, state, next_node, counters)

    async def record_event(self, lease, run_id, kind, details):
        # 调用方只能传脱敏指标/哈希，不能传凭据、完整对话或工具正文。
        async with self.transaction() as connection:
            await self.guard_run(connection, lease, run_id)
            await connection.execute("INSERT INTO ta_events(run_id,kind,details) VALUES($1,$2,$3)", run_id, kind, details)

    async def record_step(self, lease, run_id, step_id, payload_hash, status, result=None):
        if status not in ("running", "succeeded", "failed", "unknown", "skipped"):
            raise ValueError("invalid_step_status")
        async with self.transaction() as connection:
            await self.guard_run(connection, lease, run_id)
            previous = await connection.fetchrow("SELECT * FROM ta_steps WHERE run_id=$1 AND step_id=$2", run_id, step_id)
            if previous and previous["payload_hash"] != payload_hash:
                raise SharedStateError("step_payload_changed")
            if previous and previous["status"] == "succeeded":
                return previous["result"]
            await connection.execute("""INSERT INTO ta_steps(run_id,step_id,payload_hash,status,result)
                VALUES($1,$2,$3,$4,$5) ON CONFLICT(run_id,step_id) DO UPDATE
                SET status=excluded.status,result=excluded.result,updated_at=clock_timestamp()""",
                run_id, step_id, payload_hash, status, result)
            return result

    async def finish(self, lease, run_id, status, result, counters, *, data=None, revision=None):
        if status not in ("completed", "degraded", "failed", "cancelled", "timed_out"):
            raise ValueError("invalid_run_status")
        async with self.transaction() as connection:
            await self.guard_run(connection, lease, run_id)
            if data is not None:
                await self._write_session(connection, lease, data, revision)
            # 历史与最终结果在同一事务提交，不留下“历史已写、任务仍running”的窗口。
            await connection.execute("""UPDATE ta_runs SET status=$2,result=$3,counters=$4,
                updated_at=clock_timestamp() WHERE id=$1""", run_id, status, result, counters)
            await self.free_slot(connection, lease)

    async def inspect_run(self, token, run_id):
        async with self.pool.acquire(timeout=1) as connection:
            row = await connection.fetchrow("""SELECT r.* FROM ta_runs r JOIN ta_sessions s ON s.id=r.session_id
                WHERE r.id=$1 AND s.id=$2 AND s.expires_at>clock_timestamp()""", run_id, digest(token))
            if row is None:
                raise SharedStateError("task_not_found")
            return dict(row)

    async def request_cancel(self, token, run_id):
        async with self.transaction() as connection:
            # 任意实例可写取消意图；执行实例续租时读取，再停止其本地任务。
            row = await connection.fetchrow("""UPDATE ta_runs r SET cancel_requested=TRUE
                FROM ta_sessions s WHERE r.id=$1 AND r.session_id=s.id AND s.id=$2
                  AND s.expires_at>clock_timestamp() AND r.status='running' RETURNING r.id""", run_id, digest(token))
            if row is None:
                raise SharedStateError("task_not_running_or_not_owned")

    async def delete_session(self, lease):
        async with self.transaction() as connection:
            await self.guard_session(connection, lease)
            await self.free_slot(connection, lease)
            await connection.execute("DELETE FROM ta_sessions WHERE id=$1", lease.session_id)

    async def cleanup(self, *, batch_size=1000):
        if type(batch_size) is not int or not 1 <= batch_size <= 1000:
            raise ValueError("invalid_cleanup_batch")
        async with self.transaction() as connection:
            # 有界批量+SKIP LOCKED，让多个清理实例不互相等候或长时间持锁。
            rows = await connection.fetch("""DELETE FROM ta_sessions WHERE id IN (
                SELECT id FROM ta_sessions WHERE expires_at<=clock_timestamp()
                  AND (lease_until IS NULL OR lease_until<=clock_timestamp())
                ORDER BY expires_at LIMIT $1 FOR UPDATE SKIP LOCKED) RETURNING id""", batch_size)
            old_runs = await connection.fetch("""DELETE FROM ta_runs WHERE id IN (
                SELECT r.id FROM ta_runs r JOIN ta_sessions s ON s.id=r.session_id
                WHERE r.updated_at<=clock_timestamp()-interval '24 hours'
                  AND (r.status<>'running' OR s.holder IS NULL OR s.lease_until<=clock_timestamp() OR r.fence<>s.fence)
                ORDER BY r.updated_at LIMIT $1 FOR UPDATE OF r SKIP LOCKED) RETURNING id""", batch_size)
            return {"sessions": len(rows), "runs": len(old_runs)}
