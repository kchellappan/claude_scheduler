PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS usage_samples (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    sampled_at           TEXT    NOT NULL,          -- ISO8601 UTC
    ok                   INTEGER NOT NULL,          -- 1 = live reading, 0 = fetch failed
    five_hour_pct        REAL,
    five_hour_resets_at  TEXT,
    seven_day_pct        REAL,
    seven_day_resets_at  TEXT,
    extra_usage_enabled  INTEGER,
    error                TEXT,
    raw_json             TEXT
);
CREATE INDEX IF NOT EXISTS idx_samples_time ON usage_samples(sampled_at DESC);

CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt        TEXT    NOT NULL,
    working_dir   TEXT    NOT NULL,
    priority      INTEGER NOT NULL DEFAULT 100,     -- lower runs first
    status        TEXT    NOT NULL DEFAULT 'pending',
                  -- pending | running | done | failed | cancelled
    created_at    TEXT    NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER NOT NULL DEFAULT 3,
    session_id    TEXT,                             -- claude session, for resume
    exit_code     INTEGER,
    output_path   TEXT,
    error         TEXT,
    bg_id             TEXT,   -- short id from `claude --bg`, for agents/logs/stop
    bridge_session_id TEXT,   -- Remote Control session; URL is claude.ai/code/<id>
    model             TEXT,   -- NULL = whatever Claude Code defaults to
    resume_session_id TEXT,   -- NULL = fresh session; else continue this one
    source        TEXT    NOT NULL DEFAULT 'cli'    -- cli | phone | api
);
CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(status, priority, created_at);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT    NOT NULL,
    kind        TEXT    NOT NULL,
                -- gate_opened | gate_closed | queue_drained | job_done
                -- | job_failed | auth_stale | poll_failing
    payload     TEXT,                               -- JSON
    notified_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_unsent ON events(notified_at, id);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint    TEXT    NOT NULL UNIQUE,
    p256dh      TEXT    NOT NULL,
    auth        TEXT    NOT NULL,
    user_agent  TEXT,
    created_at  TEXT    NOT NULL,
    last_ok_at  TEXT,
    failures    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
