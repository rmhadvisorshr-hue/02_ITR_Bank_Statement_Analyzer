"""SQLite report-metadata store. Python port of db.mjs - same schema, same
four operations. Kept storage-engine-agnostic (plain columns, no
SQLite-specific types) for the same reason the original was: a later move to
Postgres should be a data-copy, not a schema redesign.
"""
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

_WORKSPACE = Path(__file__).resolve().parent.parent
_DATA_DIR = _WORKSPACE / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = os.environ.get("ITR_DB_PATH") or str(_DATA_DIR / "app.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    job_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    r2_key TEXT NOT NULL,
    original_filenames TEXT NOT NULL,
    generated_filename TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_user ON reports(user_id, expires_at);
"""


@contextmanager
def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


with _db() as _conn:
    _conn.executescript(_SCHEMA)


def _to_report(row):
    if row is None:
        return None
    return {
        "jobId": row["job_id"],
        "userId": row["user_id"],
        "r2Key": row["r2_key"],
        "originalFilenames": json.loads(row["original_filenames"]),
        "generatedFilename": row["generated_filename"],
        "createdAt": row["created_at"],
        "expiresAt": row["expires_at"],
    }


def insert_report(*, job_id, user_id, r2_key, original_filenames, generated_filename, created_at, expires_at):
    with _db() as conn:
        conn.execute(
            "INSERT INTO reports "
            "(job_id, user_id, r2_key, original_filenames, generated_filename, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (job_id, user_id, r2_key, json.dumps(original_filenames), generated_filename, created_at, expires_at),
        )


def get_report(job_id):
    with _db() as conn:
        row = conn.execute("SELECT * FROM reports WHERE job_id = ?", (job_id,)).fetchone()
    return _to_report(row)


def list_reports_for_user(user_id):
    now = int(time.time())
    with _db() as conn:
        conn.execute("DELETE FROM reports WHERE expires_at <= ?", (now,))
        rows = conn.execute(
            "SELECT * FROM reports WHERE user_id = ? AND expires_at > ? ORDER BY created_at DESC",
            (user_id, now),
        ).fetchall()
    return [_to_report(row) for row in rows]


def prune_expired_reports():
    """Same prune list_reports_for_user already runs inline, exposed
    standalone so the cleanup sweep (cleanup.py) can run it on a timer too.
    """
    now = int(time.time())
    with _db() as conn:
        cur = conn.execute("DELETE FROM reports WHERE expires_at <= ?", (now,))
        return cur.rowcount
