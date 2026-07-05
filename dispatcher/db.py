"""SQLite persistence: tasks (one per request) and runs (one per model attempt).

A refusal shows up as run 1 status='refused' followed by run 2 on the fallback
model — that split is what makes locked decision #3 auditable.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id         TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  source     TEXT NOT NULL,
  kind       TEXT NOT NULL,
  status     TEXT NOT NULL,
  text       TEXT NOT NULL,
  area       TEXT,
  metadata   TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, created_at);

CREATE TABLE IF NOT EXISTS runs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id       TEXT NOT NULL REFERENCES tasks(id),
  attempt       INTEGER NOT NULL DEFAULT 1,
  model         TEXT NOT NULL,
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  status        TEXT NOT NULL,
  stop_reason   TEXT,
  cost_usd      REAL,
  input_tokens  INTEGER,
  output_tokens INTEGER,
  num_turns     INTEGER,
  output_path   TEXT,
  output_text   TEXT,
  error         TEXT,
  session_id    TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- tasks -------------------------------------------------------------

    def create_task(self, text, source, kind, area=None, metadata=None) -> dict:
        task = {
            "id": uuid.uuid4().hex[:12],
            "created_at": now(),
            "source": source,
            "kind": kind,
            "status": "queued",
            "text": text,
            "area": area,
            "metadata": json.dumps(metadata) if metadata else None,
        }
        with self._conn() as c:
            c.execute(
                "INSERT INTO tasks VALUES (:id,:created_at,:source,:kind,:status,:text,:area,:metadata)",
                task,
            )
        return task

    def set_task_status(self, task_id: str, status: str):
        with self._conn() as c:
            c.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))

    def get_task(self, task_id: str) -> dict | None:
        with self._conn() as c:
            t = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not t:
                return None
            runs = c.execute(
                "SELECT * FROM runs WHERE task_id=? ORDER BY attempt", (task_id,)
            ).fetchall()
        out = dict(t)
        out["runs"] = [dict(r) for r in runs]
        return out

    def list_tasks(self, status=None, kind=None, limit=50) -> list[dict]:
        q, args = "SELECT * FROM tasks", []
        conds = []
        if status:
            conds.append("status=?"), args.append(status)
        if kind:
            conds.append("kind=?"), args.append(kind)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, args).fetchall()]

    # -- runs --------------------------------------------------------------

    def create_run(self, task_id: str, attempt: int, model: str) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO runs (task_id, attempt, model, started_at, status) VALUES (?,?,?,?,'running')",
                (task_id, attempt, model, now()),
            )
            return cur.lastrowid

    def cancel_open_runs(self, task_id: str):
        with self._conn() as c:
            c.execute(
                "UPDATE runs SET status='cancelled', finished_at=? WHERE task_id=? AND status='running'",
                (now(), task_id),
            )

    def finish_run(self, run_id: int, status: str, **fields):
        allowed = {
            "stop_reason", "cost_usd", "input_tokens", "output_tokens",
            "num_turns", "output_path", "output_text", "error", "session_id",
        }
        sets, args = ["status=?", "finished_at=?"], [status, now()]
        for k, v in fields.items():
            if k in allowed and v is not None:
                sets.append(f"{k}=?")
                args.append(v)
        args.append(run_id)
        with self._conn() as c:
            c.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id=?", args)
