"""同一 SQLite 事务中登记请求和扣次；私有检查点与脱敏审计分表保存。"""
import json
import secrets
import time

from app.core.execution import fingerprint

TERMINAL = {"completed", "degraded", "failed", "cancelled", "timed_out", "interrupted"}


class RunStore:
    def __init__(self, sessions):
        # 复用连接，避免“任务已登记但扣次失败”的跨库事务问题。仅事件循环线程访问。
        self.sessions = sessions
        self.db = sessions.db
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS task_runs (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, request_key TEXT NOT NULL,
                payload_hash TEXT NOT NULL, status TEXT NOT NULL, updated REAL NOT NULL,
                versions TEXT NOT NULL, result TEXT, checkpoint TEXT, next_node TEXT,
                counters TEXT NOT NULL DEFAULT '{}', UNIQUE(owner, request_key)
            );
            CREATE TABLE IF NOT EXISTS task_steps (
                run_id TEXT NOT NULL, step_id TEXT NOT NULL, status TEXT NOT NULL,
                payload_hash TEXT NOT NULL, result TEXT, updated REAL NOT NULL,
                PRIMARY KEY(run_id, step_id)
            );
            CREATE TABLE IF NOT EXISTS execution_events (
                run_id TEXT NOT NULL, time REAL NOT NULL, kind TEXT NOT NULL, details TEXT NOT NULL
            );
        """)
        # 本服务仅支持一个进程；重启时旧 running 不能冒充仍在执行。
        self.db.execute("UPDATE task_runs SET status='interrupted' WHERE status='running'")
        self.db.execute("UPDATE task_steps SET status='unknown' WHERE status='running'")
        self.db.commit()

    def find(self, token, request_key, question):
        row = self.db.execute("SELECT id,payload_hash FROM task_runs WHERE owner=? AND request_key=?",
                              (fingerprint(token), request_key)).fetchone()
        if not row:
            return None
        if row[1] != fingerprint(question):
            raise ValueError("idempotency_conflict")
        return self.get(token, row[0])

    def begin(self, token, request_key, question, versions, daily_limit):
        run_id = secrets.token_hex(16)
        with self.db:
            # reserve_request 的独立 commit 不适用；这里直接在同一事务内扣每日次数。
            from datetime import datetime, timezone
            day = datetime.now(timezone.utc).date().isoformat()
            self.db.execute("INSERT OR IGNORE INTO budget VALUES (?,0)", (day,))
            cursor = self.db.execute("UPDATE budget SET count=count+1 WHERE day=? AND count<?", (day, daily_limit))
            if cursor.rowcount != 1:
                raise RuntimeError("daily_limit")
            self.db.execute("INSERT INTO task_runs(id,owner,request_key,payload_hash,status,updated,versions) VALUES (?,?,?,?,?,?,?)",
                            (run_id, fingerprint(token), request_key, fingerprint(question), "running", time.time(), json.dumps(versions)))
        return run_id

    def get(self, token, run_id):
        row = self.db.execute("SELECT id,status,updated,versions,result,checkpoint,next_node,counters FROM task_runs WHERE id=? AND owner=?",
                              (run_id, fingerprint(token))).fetchone()
        if not row:
            raise LookupError("task_not_found")
        result = dict(zip(("id", "status", "updated", "versions", "result", "checkpoint", "next_node", "counters"), row))
        for key in ("versions", "result", "checkpoint", "counters"):
            result[key] = json.loads(result[key]) if result[key] else None
        return result

    def event(self, run_id, kind, details):
        self.db.execute("INSERT INTO execution_events VALUES (?,?,?,?)", (run_id, time.time(), kind, json.dumps(details)))
        self.db.commit()

    def checkpoint(self, run_id, state, next_node, counters):
        self.db.execute("UPDATE task_runs SET checkpoint=?,next_node=?,counters=?,updated=? WHERE id=?",
                        (json.dumps(state, ensure_ascii=False), next_node, json.dumps(counters), time.time(), run_id))
        self.db.commit()

    def counters(self, run_id, counters):
        self.db.execute("UPDATE task_runs SET counters=?,updated=? WHERE id=?", (json.dumps(counters), time.time(), run_id))
        self.db.commit()

    def claim_resume(self, run_id):
        cursor = self.db.execute("UPDATE task_runs SET status='running' WHERE id=? AND status IN ('interrupted','timed_out')", (run_id,))
        self.db.commit()
        if cursor.rowcount != 1:
            raise RuntimeError("task_not_resumable")

    def finish(self, run_id, status, result, counters):
        if status not in TERMINAL:
            raise ValueError("invalid_task_status")
        self.db.execute("UPDATE task_runs SET status=?,result=?,counters=?,updated=? WHERE id=?",
                        (status, json.dumps(result, ensure_ascii=False), json.dumps(counters), time.time(), run_id))
        self.db.commit()

    def step(self, run_id, step_id, payload_hash):
        row = self.db.execute("SELECT status,payload_hash,result FROM task_steps WHERE run_id=? AND step_id=?", (run_id, step_id)).fetchone()
        if row and row[1] != payload_hash:
            raise ValueError("step_payload_changed")
        if row and row[0] == "succeeded":
            return json.loads(row[2])
        return None

    def save_step(self, run_id, step_id, payload_hash, status, result=None):
        self.db.execute("INSERT OR REPLACE INTO task_steps VALUES (?,?,?,?,?,?)",
                        (run_id, step_id, status, payload_hash, json.dumps(result) if result is not None else None, time.time()))
        self.db.commit()

    def recent_result(self, run_id, payload_hash):
        row = self.db.execute("SELECT result FROM task_steps WHERE run_id=? AND payload_hash=? AND status='succeeded' AND updated>? ORDER BY updated DESC LIMIT 1",
                              (run_id, payload_hash, time.time() - 30)).fetchone()
        return json.loads(row[0]) if row else None

    def by_request_key(self, token, request_key):
        row = self.db.execute("SELECT id FROM task_runs WHERE owner=? AND request_key=?", (fingerprint(token), request_key)).fetchone()
        if not row:
            raise LookupError("task_not_found")
        return self.get(token, row[0])

    def erase(self, token):
        owner = fingerprint(token)
        with self.db:
            for table in ("task_steps", "execution_events"):
                self.db.execute(f"DELETE FROM {table} WHERE run_id IN (SELECT id FROM task_runs WHERE owner=?)", (owner,))
            self.db.execute("DELETE FROM task_runs WHERE owner=?", (owner,))

    def cleanup(self):
        # 与会话相同的24小时闲置保留期；过期时连同结果/工具正文一并清理。
        expired = self.db.execute("SELECT token FROM sessions WHERE updated<?", (time.time() - 86400,)).fetchall()
        for row in expired:
            self.erase(row[0])
        with self.db:
            cutoff = time.time() - 86400
            for table in ("task_steps", "execution_events"):
                self.db.execute(f"DELETE FROM {table} WHERE run_id IN (SELECT id FROM task_runs WHERE updated<?)", (cutoff,))
            self.db.execute("DELETE FROM task_runs WHERE updated<?", (cutoff,))

    def audit(self, run_id):
        rows = self.db.execute("SELECT time,kind,details FROM execution_events WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
        return [{"time": row[0], "kind": row[1], **json.loads(row[2])} for row in rows]
