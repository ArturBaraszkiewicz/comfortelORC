from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}


class Store:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA foreign_keys = ON")
                yield db
        finally:
            db.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, created_at TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'member'
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, username TEXT NOT NULL REFERENCES users(username),
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS login_attempts (address TEXT NOT NULL, attempted_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL UNIQUE,
                    instructions TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                    remote_url TEXT NOT NULL DEFAULT '', branch TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    title TEXT NOT NULL, prompt TEXT NOT NULL, provider TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL,
                    timeout_minutes INTEGER NOT NULL, status TEXT NOT NULL,
                    base_commit TEXT NOT NULL, workspace TEXT, branch TEXT,
                    created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    result TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                    exit_code INTEGER, cost_usd REAL, input_tokens INTEGER, output_tokens INTEGER,
                    diff TEXT NOT NULL DEFAULT '', diff_truncated INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id),
                    kind TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_events_job ON events(job_id, id);
                CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status, created_at);
            """)
            # Additive migrations keep data created by earlier local builds usable.
            for table, column, definition in (
                ("users", "role", "TEXT NOT NULL DEFAULT 'member'"),
                ("projects", "remote_url", "TEXT NOT NULL DEFAULT ''"),
                ("projects", "branch", "TEXT NOT NULL DEFAULT ''"),
                ("jobs", "created_by", "TEXT NOT NULL DEFAULT ''"),
            ):
                columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                if column not in columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def recover(self):
        with self.connect() as db:
            db.execute("""UPDATE jobs SET status='interrupted', finished_at=?,
                error='Aplikacja została zamknięta w trakcie zadania. Uruchom je ponownie.'
                WHERE status='running'""", (now(),))
            # Do not silently execute persisted tasks merely because the app starts.
            db.execute("""UPDATE jobs SET status='interrupted', finished_at=?,
                error='Zadanie oczekiwało przed restartem. Uruchom je ponownie.'
                WHERE status='queued'""", (now(),))

    def projects(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM projects ORDER BY name")]

    def project(self, project_id: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            return dict(row) if row else None

    def add_project(self, name: str, path: str, instructions: str, remote_url="", branch=""):
        project_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO projects (id, name, path, instructions, created_at, remote_url, branch) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (project_id, name, path, instructions, now(), remote_url, branch))
        return self.project(project_id)

    def add_job(self, project_id, title, prompt, provider, model, kind, timeout_minutes, base_commit, created_by=""):
        job_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("""INSERT INTO jobs
                (id, project_id, title, prompt, provider, model, kind, timeout_minutes,
                 base_commit, status, created_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
                (job_id, project_id, title, prompt, provider, model, kind, timeout_minutes, base_commit, now(), created_by))
        self.event(job_id, "status", "Zadanie dodane do kolejki.")
        return self.job(job_id)

    def jobs(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("""SELECT j.id, j.project_id, p.name AS project_name,
                j.title, j.provider, j.kind, j.status, j.created_at, j.started_at, j.finished_at,
                j.cancel_requested, j.created_by FROM jobs j JOIN projects p ON p.id=j.project_id
                ORDER BY j.created_at DESC LIMIT 200""")]

    def job(self, job_id: str):
        with self.connect() as db:
            row = db.execute("""SELECT j.*, p.name AS project_name, p.path AS project_path,
                p.instructions FROM jobs j JOIN projects p ON p.id=j.project_id WHERE j.id=?""",
                (job_id,)).fetchone()
            return dict(row) if row else None

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET status='running', started_at=? WHERE id=?", (now(), row["id"]))
        return self.job(row["id"])

    def update(self, job_id: str, **fields):
        allowed = {"workspace", "branch", "result", "error", "exit_code", "cost_usd",
                   "input_tokens", "output_tokens", "diff", "diff_truncated"}
        if not fields or not fields.keys() <= allowed:
            raise ValueError("Invalid job update")
        columns = ", ".join(f"{key}=?" for key in fields)
        with self.connect() as db:
            db.execute(f"UPDATE jobs SET {columns} WHERE id=?", (*fields.values(), job_id))

    def finish(self, job_id: str, status: str, error: str = ""):
        if status not in TERMINAL:
            raise ValueError(status)
        with self.connect() as db:
            db.execute("""UPDATE jobs SET status=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE ? END,
                error=?, finished_at=? WHERE id=? AND status='running'""", (status, error, now(), job_id))

    def cancel(self, job_id: str):
        with self.connect() as db:
            db.execute("""UPDATE jobs SET cancel_requested=1,
                finished_at=CASE WHEN status='queued' THEN ? ELSE finished_at END,
                status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END
                WHERE id=? AND status IN ('queued', 'running')""", (now(), job_id))
        return self.job(job_id)

    def event(self, job_id: str, kind: str, text: str):
        with self.connect() as db:
            db.execute("INSERT INTO events (job_id, kind, text, created_at) VALUES (?, ?, ?, ?)",
                       (job_id, kind, text[:32000], now()))

    def events(self, job_id: str, after: int = 0):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM events WHERE job_id=? AND id>? ORDER BY id LIMIT 250", (job_id, after))]
