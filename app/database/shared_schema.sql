-- 独立 PostgreSQL 数据库；不能在课程库或现有 SQLite 上执行。
-- 初始化只建表，不把其他实例的 running 任务批量改成 interrupted。
CREATE SEQUENCE IF NOT EXISTS ta_fence;

CREATE TABLE IF NOT EXISTS ta_settings (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    capacity INTEGER NOT NULL,
    max_sessions INTEGER NOT NULL,
    lease_seconds DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS ta_sessions (
    id TEXT PRIMARY KEY,
    data JSONB NOT NULL,
    revision BIGINT NOT NULL DEFAULT 0,
    expires_at TIMESTAMPTZ NOT NULL,
    holder TEXT,
    fence BIGINT NOT NULL DEFAULT 0,
    lease_until TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ta_sessions_expiry ON ta_sessions(expires_at);

CREATE TABLE IF NOT EXISTS ta_budget (
    day DATE PRIMARY KEY,
    count BIGINT NOT NULL CHECK (count >= 0)
);
CREATE TABLE IF NOT EXISTS ta_runs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES ta_sessions(id) ON DELETE CASCADE,
    request_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    fence BIGINT NOT NULL,
    status TEXT NOT NULL,
    versions JSONB NOT NULL,
    counters JSONB NOT NULL DEFAULT '{}',
    result JSONB,
    checkpoint JSONB,
    next_node TEXT,
    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(session_id, request_key)
);
CREATE INDEX IF NOT EXISTS ta_runs_updated ON ta_runs(updated_at);
CREATE TABLE IF NOT EXISTS ta_slots (
    id INTEGER PRIMARY KEY,
    session_id TEXT REFERENCES ta_sessions(id) ON DELETE SET NULL,
    holder TEXT,
    fence BIGINT,
    lease_until TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS ta_steps (
    run_id TEXT NOT NULL REFERENCES ta_runs(id) ON DELETE CASCADE,
    step_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    result JSONB,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(run_id, step_id)
);
CREATE TABLE IF NOT EXISTS ta_events (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES ta_runs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    details JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS ta_events_run ON ta_events(run_id, id);
