"""SQLite persistence: tasks (one per request) and runs (one per model attempt).

A refusal shows up as run 1 status='refused' followed by run 2 on the fallback
model — that split is what makes locked decision #3 auditable.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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

-- Memory (Phase F). episodes = raw interaction log, one row per settled task;
-- valid_at is when it happened (bi-temporal capture per graphiti — the one
-- thing that can't be retrofitted). consolidated_at marks hand-off to the
-- nightly consolidation agent.
CREATE TABLE IF NOT EXISTS episodes (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id         TEXT,
  source          TEXT NOT NULL,
  kind            TEXT NOT NULL,
  area            TEXT,
  status          TEXT NOT NULL,
  user_text       TEXT NOT NULL,
  assistant_text  TEXT,
  valid_at        TEXT NOT NULL,
  consolidated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_episodes_consolidated ON episodes(consolidated_at);
CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(text, episode_id UNINDEXED);

-- Vault search index: heading-ancestry chunks with per-chunk content hashes
-- (ingest design after khoj's TextToEntries — AGPL, patterns re-implemented,
-- no code copied). entries_fts is a standalone FTS5 copy: duplicates a small
-- corpus in exchange for not managing external-content sync triggers.
CREATE TABLE IF NOT EXISTS vault_files (
  path       TEXT PRIMARY KEY,
  mtime      REAL NOT NULL,
  indexed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entries (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  file_path  TEXT NOT NULL,
  heading    TEXT,
  line_no    INTEGER,
  raw        TEXT NOT NULL,
  compiled   TEXT NOT NULL,
  hash       TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_file ON entries(file_path);
CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(compiled, entry_id UNINDEXED);
CREATE TABLE IF NOT EXISTS entry_dates (
  entry_id INTEGER NOT NULL,
  date     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entry_dates ON entry_dates(date);

-- Skill lifecycle telemetry (Phase G, after hermes-agent tools/skill_usage.py,
-- MIT): one row per area skill; the deterministic curator ages state
-- active → stale → archived from these timestamps. Best-effort counters —
-- a failed bump must never fail the task that caused it.
CREATE TABLE IF NOT EXISTS skill_usage (
  name            TEXT PRIMARY KEY,
  use_count       INTEGER NOT NULL DEFAULT 0,
  patch_count     INTEGER NOT NULL DEFAULT 0,
  last_used_at    TEXT,
  last_patched_at TEXT,
  first_seen_at   TEXT NOT NULL,
  state           TEXT NOT NULL DEFAULT 'active',
  pinned          INTEGER NOT NULL DEFAULT 0
);

-- Per-run timeline (Phase H, shape after openjarvis traces/store.py
-- trace_steps, Apache-2.0): one row per stream-json event of an agentic run.
CREATE TABLE IF NOT EXISTS run_steps (
  run_id     INTEGER NOT NULL,
  step_index INTEGER NOT NULL,
  step_type  TEXT NOT NULL,
  elapsed_ms INTEGER,
  summary    TEXT,
  PRIMARY KEY (run_id, step_index)
);
"""

# Additive, idempotent column migrations (openjarvis _MIGRATE_COLUMNS habit):
# each statement may fail with "duplicate column name" on an already-migrated
# db — that's the expected steady state.
MIGRATIONS = [
    "ALTER TABLE runs ADD COLUMN ttft_ms REAL",
    "ALTER TABLE runs ADD COLUMN itl_p95_ms REAL",
    "ALTER TABLE runs ADD COLUMN tokens_per_s REAL",
]


def fts_query(q: str) -> str:
    """Every term double-quoted so user text can't hit FTS5 operator syntax
    (NEAR, AND, column filters, unbalanced quotes)."""
    terms = [t.replace('"', "") for t in q.split()]
    return " ".join(f'"{t}"' for t in terms if t)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            for stmt in MIGRATIONS:
                try:
                    c.execute(stmt)
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e):
                        raise

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

    def reconcile_orphans(self) -> int:
        """Close out tasks/runs a previous process left non-terminal. Called
        once at startup, before any new work is accepted — at that moment
        every 'queued'/'running' row is an orphan from a dead dispatcher."""
        with self._conn() as c:
            c.execute(
                "UPDATE runs SET status='failed', finished_at=?, error=? WHERE status='running'",
                (now(), "interrupted: dispatcher stopped mid-run"),
            )
            cur = c.execute(
                "UPDATE tasks SET status='failed' WHERE status IN ('queued','running')"
            )
            return cur.rowcount

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
            "ttft_ms", "itl_p95_ms", "tokens_per_s",
        }
        sets, args = ["status=?", "finished_at=?"], [status, now()]
        for k, v in fields.items():
            if k in allowed and v is not None:
                sets.append(f"{k}=?")
                args.append(v)
        args.append(run_id)
        with self._conn() as c:
            c.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id=?", args)

    # -- run steps (Phase H timeline) --------------------------------------

    def add_run_steps(self, run_id: int, steps: list[dict]):
        if not steps:
            return
        with self._conn() as c:
            c.executemany(
                "INSERT OR REPLACE INTO run_steps"
                " (run_id, step_index, step_type, elapsed_ms, summary)"
                " VALUES (?,?,?,?,?)",
                [(run_id, i, s["type"], s.get("elapsed_ms"), s.get("summary"))
                 for i, s in enumerate(steps)])

    def get_run_steps(self, run_id: int) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM run_steps WHERE run_id=? ORDER BY step_index",
                (run_id,)).fetchall()]

    def stats_summary(self, days: int = 7) -> dict:
        """Aggregates for /stats — pure SQL, one dict out (openjarvis
        analyzer/aggregator shape, hermes insights spirit)."""
        since = (datetime.now(timezone.utc)
                 .replace(hour=0, minute=0, second=0, microsecond=0))
        since_iso = (since - timedelta(days=days - 1)).isoformat(timespec="seconds")
        with self._conn() as c:
            by_status = {r["status"]: r["n"] for r in c.execute(
                "SELECT status, COUNT(*) n FROM tasks WHERE created_at>=?"
                " GROUP BY status", (since_iso,))}
            by_source = [dict(r) for r in c.execute(
                "SELECT t.source, COUNT(*) n, ROUND(SUM(COALESCE(r.cost_usd,0)),4) cost_usd"
                " FROM tasks t LEFT JOIN runs r ON r.task_id=t.id"
                " WHERE t.created_at>=? GROUP BY t.source ORDER BY n DESC",
                (since_iso,))]
            latency = c.execute(
                "SELECT ROUND(AVG(r.ttft_ms),1) avg_ttft_ms,"
                " ROUND(AVG(r.tokens_per_s),2) avg_tokens_per_s, COUNT(*) n"
                " FROM runs r JOIN tasks t ON t.id=r.task_id"
                " WHERE t.kind='quick' AND r.ttft_ms IS NOT NULL"
                " AND r.started_at>=?", (since_iso,)).fetchone()
            reflections = [dict(r) for r in c.execute(
                "SELECT r.finished_at, r.output_text FROM runs r"
                " JOIN tasks t ON t.id=r.task_id WHERE t.source='reflection'"
                " AND r.status='done' ORDER BY r.id DESC LIMIT 5")]
            cost = c.execute(
                "SELECT ROUND(SUM(COALESCE(cost_usd,0)),4) total FROM runs"
                " WHERE started_at>=?", (since_iso,)).fetchone()
        done = by_status.get("done", 0)
        failed = by_status.get("failed", 0)
        return {
            "days": days,
            "tasks_by_status": by_status,
            "by_source": by_source,
            "success_rate": round(done / (done + failed), 3) if done + failed else None,
            "total_cost_usd": cost["total"] or 0,
            "quick_latency": dict(latency) if latency and latency["n"] else None,
            "recent_reflections": reflections,
        }

    # -- episodes (memory capture) -----------------------------------------

    def add_episode(self, task_id, source, kind, area, status,
                    user_text, assistant_text) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO episodes (task_id, source, kind, area, status,"
                " user_text, assistant_text, valid_at) VALUES (?,?,?,?,?,?,?,?)",
                (task_id, source, kind, area, status, user_text,
                 assistant_text, now()),
            )
            eid = cur.lastrowid
            c.execute(
                "INSERT INTO episodes_fts (text, episode_id) VALUES (?,?)",
                (f"{user_text}\n{assistant_text or ''}", eid),
            )
            return eid

    def unconsolidated_episodes(self, limit: int = 200) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM episodes WHERE consolidated_at IS NULL"
                " ORDER BY id LIMIT ?", (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_episodes_consolidated(self, ids: list[int]):
        if not ids:
            return
        with self._conn() as c:
            c.executemany(
                "UPDATE episodes SET consolidated_at=? WHERE id=?",
                [(now(), i) for i in ids],
            )

    def search_episodes(self, q: str, limit: int = 10,
                        after: str | None = None,
                        before: str | None = None) -> list[dict]:
        match = fts_query(q)
        if not match:
            return []
        sql = (
            "SELECT e.id, e.task_id, e.source, e.kind, e.area, e.status,"
            " e.user_text, e.assistant_text, e.valid_at,"
            " bm25(episodes_fts) AS score"
            " FROM episodes_fts f JOIN episodes e ON e.id = f.episode_id"
            " WHERE episodes_fts MATCH ?"
        )
        args: list = [match]
        if after:
            sql += " AND e.valid_at >= ?"
            args.append(after)
        if before:
            sql += " AND e.valid_at <= ?"
            args.append(before)
        sql += " ORDER BY score LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    # -- skill usage (Phase G) ---------------------------------------------

    def _touch_skill(self, c, name: str):
        c.execute(
            "INSERT INTO skill_usage (name, first_seen_at) VALUES (?,?)"
            " ON CONFLICT(name) DO NOTHING", (name, now()))

    def record_skill_use(self, name: str):
        with self._conn() as c:
            self._touch_skill(c, name)
            c.execute(
                "UPDATE skill_usage SET use_count=use_count+1, last_used_at=?,"
                " state=CASE WHEN state='stale' THEN 'active' ELSE state END"
                " WHERE name=?", (now(), name))

    def record_skill_patch(self, name: str):
        with self._conn() as c:
            self._touch_skill(c, name)
            c.execute(
                "UPDATE skill_usage SET patch_count=patch_count+1,"
                " last_patched_at=?,"
                " state=CASE WHEN state='stale' THEN 'active' ELSE state END"
                " WHERE name=?", (now(), name))

    def skill_usage_all(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in
                    c.execute("SELECT * FROM skill_usage ORDER BY name").fetchall()]

    def set_skill_state(self, name: str, state: str):
        with self._conn() as c:
            self._touch_skill(c, name)
            c.execute("UPDATE skill_usage SET state=? WHERE name=?", (state, name))

    # -- vault entries (search index) --------------------------------------

    def vault_file_mtimes(self) -> dict[str, float]:
        with self._conn() as c:
            rows = c.execute("SELECT path, mtime FROM vault_files").fetchall()
            return {r["path"]: r["mtime"] for r in rows}

    def replace_file_entries(self, path: str, mtime: float,
                             chunks: list[dict]) -> tuple[int, int]:
        """Hash-diff one file's chunks against what's indexed: only chunks
        whose content hash is new get inserted, vanished hashes get deleted,
        the rest are untouched (khoj update_embeddings pattern, re-implemented).
        chunks: [{heading, line_no, raw, compiled, hash, dates}]."""
        new_by_hash = {ch["hash"]: ch for ch in chunks}
        added = deleted = 0
        with self._conn() as c:
            existing = c.execute(
                "SELECT id, hash FROM entries WHERE file_path=?", (path,)
            ).fetchall()
            seen = set()
            for row in existing:
                if row["hash"] in new_by_hash and row["hash"] not in seen:
                    seen.add(row["hash"])
                else:  # gone from the file (or a duplicate row): drop it
                    c.execute("DELETE FROM entries WHERE id=?", (row["id"],))
                    c.execute("DELETE FROM entries_fts WHERE entry_id=?", (row["id"],))
                    c.execute("DELETE FROM entry_dates WHERE entry_id=?", (row["id"],))
                    deleted += 1
            for ch in chunks:
                if ch["hash"] in seen:
                    continue
                seen.add(ch["hash"])
                cur = c.execute(
                    "INSERT INTO entries (file_path, heading, line_no, raw,"
                    " compiled, hash, created_at) VALUES (?,?,?,?,?,?,?)",
                    (path, ch.get("heading"), ch.get("line_no"), ch["raw"],
                     ch["compiled"], ch["hash"], now()),
                )
                eid = cur.lastrowid
                c.execute("INSERT INTO entries_fts (compiled, entry_id) VALUES (?,?)",
                          (ch["compiled"], eid))
                for d in ch.get("dates") or []:
                    c.execute("INSERT INTO entry_dates (entry_id, date) VALUES (?,?)",
                              (eid, d))
                added += 1
            c.execute(
                "INSERT INTO vault_files (path, mtime, indexed_at) VALUES (?,?,?)"
                " ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime,"
                " indexed_at=excluded.indexed_at",
                (path, mtime, now()),
            )
        return added, deleted

    def delete_file_entries(self, path: str) -> int:
        with self._conn() as c:
            ids = [r["id"] for r in c.execute(
                "SELECT id FROM entries WHERE file_path=?", (path,)).fetchall()]
            for eid in ids:
                c.execute("DELETE FROM entries_fts WHERE entry_id=?", (eid,))
                c.execute("DELETE FROM entry_dates WHERE entry_id=?", (eid,))
            c.execute("DELETE FROM entries WHERE file_path=?", (path,))
            c.execute("DELETE FROM vault_files WHERE path=?", (path,))
            return len(ids)

    def search_entries(self, q: str, limit: int = 10,
                       file_like: str | None = None,
                       after: str | None = None,
                       before: str | None = None) -> list[dict]:
        match = fts_query(q)
        if not match:
            return []
        sql = (
            "SELECT e.id, e.file_path, e.heading, e.line_no, e.raw,"
            " bm25(entries_fts) AS score"
            " FROM entries_fts f JOIN entries e ON e.id = f.entry_id"
            " WHERE entries_fts MATCH ?"
        )
        args: list = [match]
        if file_like:
            sql += " AND e.file_path LIKE ?"
            args.append(file_like.replace("*", "%"))
        if after or before:
            dsql = "SELECT entry_id FROM entry_dates WHERE 1=1"
            if after:
                dsql += " AND date >= ?"
                args.append(after)
            if before:
                dsql += " AND date <= ?"
                args.append(before)
            sql += f" AND e.id IN ({dsql})"
        sql += " ORDER BY score LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]
