"""SQLite seam. Every process talks through this; nothing else opens the file."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# Columns added after the first release. CREATE TABLE IF NOT EXISTS will not
# add them to a database that already exists, so apply them explicitly.
_MIGRATIONS = [
    ("jobs", "bg_id", "TEXT"),
    ("jobs", "bridge_session_id", "TEXT"),
]


def _migrate(conn) -> None:
    for table, column, decl in _MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init() -> None:
    schema = (Path(__file__).parent / "schema.sql").read_text()
    with connect() as conn:
        conn.executescript(schema)
        _migrate(conn)


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn, key, value) -> None:
    conn.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def emit(conn, kind: str, **payload) -> None:
    """Record a domain event. The notifier drains these; nothing pushes inline."""
    conn.execute(
        "INSERT INTO events(created_at, kind, payload) VALUES(?,?,?)",
        (utcnow(), kind, json.dumps(payload) if payload else None),
    )


def latest_sample(conn):
    return conn.execute(
        "SELECT * FROM usage_samples WHERE ok=1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
