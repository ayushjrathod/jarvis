"""SQLite persistence: tasks (one per request) and runs (one per model attempt).

A refusal shows up as run 1 status='refused' followed by run 2 on the fallback
model — that split is what makes locked decision #3 auditable.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("dispatcher.db")

# WAL single-writer contention waits this long before raising OperationalError.
# Doubles as the sqlite3.connect() busy timeout (seconds) and PRAGMA (ms).
BUSY_TIMEOUT_S = 15

try:
    import sqlite_vec
    _VEC_AVAILABLE = True
except ImportError:
    sqlite_vec = None
    _VEC_AVAILABLE = False

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

-- Standing automations (Phase I, khoj automations pattern re-implemented):
-- schedule fields are local-time (at_time HH:MM, weekday 0=Monday);
-- next_run_at is UTC ISO so the scheduler compares against now() strings.
-- NULL next_run_at = spent (a fired 'once') — never due again.
CREATE TABLE IF NOT EXISTS automations (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at       TEXT NOT NULL,
  source           TEXT NOT NULL,
  request          TEXT NOT NULL,
  task_text        TEXT NOT NULL,
  kind             TEXT NOT NULL,
  at_time          TEXT,
  weekday          INTEGER,
  interval_minutes INTEGER,
  once_at          TEXT,
  next_run_at      TEXT,
  last_run_at      TEXT,
  last_task_id     TEXT,
  enabled          INTEGER NOT NULL DEFAULT 1,
  notify           INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_automations_due ON automations(enabled, next_run_at);

-- Knowledge graph (Phase J2, graphiti bi-temporal pattern, Apache-2.0):
-- facts are standalone sentences linked n-ary to entities. valid_at = true
-- in the world since; invalid_at = contradicted as of; expired_at = when we
-- learned it. Invalidation never deletes — history stays queryable.
CREATE TABLE IF NOT EXISTS kg_entities (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kg_facts (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  fact        TEXT NOT NULL,
  valid_at    TEXT,
  invalid_at  TEXT,
  expired_at  TEXT,
  episode_ids TEXT,
  created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kg_fact_entities (
  fact_id   INTEGER NOT NULL,
  entity_id INTEGER NOT NULL,
  PRIMARY KEY (fact_id, entity_id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS kg_facts_fts USING fts5(fact, fact_id UNINDEXED);
"""

# Additive, idempotent column migrations (openjarvis _MIGRATE_COLUMNS habit):
# each statement may fail with "duplicate column name" on an already-migrated
# db — that's the expected steady state.
MIGRATIONS = [
    "ALTER TABLE runs ADD COLUMN ttft_ms REAL",
    "ALTER TABLE runs ADD COLUMN itl_p95_ms REAL",
    "ALTER TABLE runs ADD COLUMN tokens_per_s REAL",
    # When this fact was last shown to a model as an invalidation candidate.
    # NULL = never offered, which is where every pre-2026-08-10 fact starts —
    # additive and nullable, so an existing db needs nothing but the ALTER.
    "ALTER TABLE kg_facts ADD COLUMN last_offered_at TEXT",
    # When this episode's facts were extracted into the knowledge graph.
    # Tracked SEPARATELY from consolidated_at because the two hand-offs fail
    # independently: /memory/consolidate marks the batch and spawns both the
    # consolidation agent and graph extraction over it, and a failed
    # consolidation rolls consolidated_at back — returning episodes the
    # extractor had already read successfully. NULL = never extracted, which is
    # where every pre-2026-08-11 episode starts.
    "ALTER TABLE episodes ADD COLUMN graph_extracted_at TEXT",
    # Which embedding model wrote the vectors. A different 384-dim model
    # (all-MiniLM-L6-v2) used to be accepted silently, coexisting in an
    # incompatible space with no re-embed trigger — search answered from
    # stale vectors every 15 minutes while logging backfill failure for a
    # non-384-dim one. CREATE IF NOT EXISTS: runs clean on new and old DBs.
    "CREATE TABLE IF NOT EXISTS embedding_meta (k TEXT PRIMARY KEY, v TEXT)",
]

# Share of a candidate window reserved for the NEWEST active facts. Today's
# episodes usually contradict something recent, so that half keeps the old
# id-DESC behaviour; the other half rotates oldest-offered-first so no fact is
# immortal (see candidate_facts).
CANDIDATE_RECENT_SHARE = 0.5

# Vector mirrors (Phase J1, sqlite-vec): rowid == the entries/episodes id, so
# deletion is a rowid DELETE and KNN results join straight back. 384 dims =
# bge-small-en-v1.5 (dispatcher/embeddings.py must agree). Created only when
# the sqlite-vec extension loads; everything degrades to FTS-only without it.
SCHEMA_VEC = """
CREATE VIRTUAL TABLE IF NOT EXISTS entries_vec USING vec0(embedding float[384]);
CREATE VIRTUAL TABLE IF NOT EXISTS episodes_vec USING vec0(embedding float[384]);
"""


def _rrf(ranklists: list[list], k: int = 60) -> tuple[list, dict]:
    """Reciprocal-rank fusion (graphiti's ~15-line pattern, Apache-2.0):
    items ranked high in ANY list bubble up; k=60 is the standard damping."""
    scores: dict = {}
    for ranks in ranklists:
        for i, item in enumerate(ranks):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + i + 1)
    return sorted(scores, key=scores.get, reverse=True), scores


def fts_query(q: str) -> str:
    """Every term double-quoted so user text can't hit FTS5 operator syntax
    (NEAR, AND, column filters, unbalanced quotes)."""
    terms = [t.replace('"', "") for t in q.split()]
    return " ".join(f'"{t}"' for t in terms if t)


def fts_query_any(q: str) -> str:
    """OR-joined variant for short documents (kg facts): a question like
    "where does mira live" must hit "Mira lives in Pune." even though most
    query words are absent — AND semantics returns nothing there. BM25 still
    ranks fuller matches first."""
    terms = [t.replace('"', "") for t in q.split()]
    return " OR ".join(f'"{t}"' for t in terms if t)


def _fts_with_fallback(c, sql: str, args: list, q: str):
    """Run an AND-joined FTS query; on zero rows retry OR-joined.

    FTS5 joins space-separated terms with implicit AND, so every
    natural-language question ('what is my preferred coding tool') demanded
    all its words — stopwords included — in one chunk and returned nothing,
    leaving hybrid search as pure KNN. AND-first preserves exact behavior
    wherever it hits; the OR leg only fires on a miss, where BM25 still ranks
    the fullest match first."""
    rows = c.execute(sql, args).fetchall()
    if not rows:
        any_match = fts_query_any(q)
        if any_match and any_match != args[0]:
            rows = c.execute(sql, [any_match, *args[1:]]).fetchall()
    return rows


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_date(s) -> bool:
    """A YYYY-MM-DD string (the valid_at shape), nothing else."""
    if not isinstance(s, str) or len(s.strip()) != 10:
        return False
    try:
        date.fromisoformat(s.strip())
        return True
    except ValueError:
        return False


def local_today() -> str:
    """Local YYYY-MM-DD for anything a human reads as "today".

    `now()` is UTC; the timers fire on local wall-clock (02:30 local is 21:00
    UTC the previous day at +5:30), so stamping a prompt with `now()[:10]`
    told every nightly agent today was yesterday. Naive `datetime.now()` is
    the same clock systemd OnCalendar and scripts/run_agent.py use, so the
    brief and the memory blocks finally agree on what day it is."""
    return datetime.now().date().isoformat()


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.vec_ok = _VEC_AVAILABLE
        with self._conn() as c:
            c.executescript(SCHEMA)
            for stmt in MIGRATIONS:
                try:
                    c.execute(stmt)
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e):
                        raise
        if self.vec_ok:
            try:
                with self._conn() as c:
                    c.executescript(SCHEMA_VEC)
            except sqlite3.OperationalError as e:
                log.warning("sqlite-vec unusable (%s); hybrid search disabled", e)
                self.vec_ok = False

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_S)
        conn.row_factory = sqlite3.Row
        if getattr(self, "vec_ok", False):
            try:
                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
                conn.enable_load_extension(False)
            except Exception:
                self.vec_ok = False
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        # WAL still serializes writers; a longer busy_timeout makes a contended
        # write wait at SQLite instead of raising OperationalError at the driver
        # default — so a settle-path write (finish_run/set_task_status) can't
        # strand a task 'running' just because two writers overlapped briefly.
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_S * 1000}")
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

    def orphaned_consolidation_episode_ids(self) -> list[int]:
        """Episode ids held by consolidation tasks that never settled 'done'.

        Episodes are marked consolidated at hand-off, so a consolidation
        interrupted by a restart (suspend at 02:30, a crash, a kill) strands
        that whole batch: it is neither distilled nor eligible for the next
        pass, and the only trace is an export file nobody is prompted to
        replay. Read AFTER reconcile_orphans has settled those tasks as
        failed, so this is "every consolidation that did not finish", not just
        the ones from this boot. Cheap: consolidation tasks are one a night.
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT metadata FROM tasks"
                " WHERE json_extract(metadata,'$.task_type')='memory-consolidate'"
                "   AND status!='done'"
                # source lives in its own column, outside caller-settable
                # metadata: only the timer's own consolidation may hold
                # episodes hostage (added 2026-08-06, finding 2.4).
                "   AND source='timer'"
            ).fetchall()
        ids: list[int] = []
        for r in rows:
            try:
                ids.extend(json.loads(r["metadata"]).get("episode_ids") or [])
            except (TypeError, ValueError):
                continue
        return ids

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

    def list_tasks(self, status=None, kind=None, limit=50, source=None) -> list[dict]:
        q, args = "SELECT * FROM tasks", []
        conds = []
        if status:
            conds.append("status=?"), args.append(status)
        if kind:
            conds.append("kind=?"), args.append(kind)
        if source:
            conds.append("source=?"), args.append(source)
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
                # COUNT(DISTINCT t.id), not COUNT(*): the LEFT JOIN fans a task
                # out across its runs, so a refusal-fallback or a resume-retry
                # counted the same task twice and `n` silently meant "runs"
                "SELECT t.source, COUNT(DISTINCT t.id) n,"
                " ROUND(SUM(COALESCE(r.cost_usd,0)),4) cost_usd"
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

    def mark_episodes_unconsolidated(self, ids: list[int]) -> int:
        """Undo a hand-off mark (M6): when the consolidation run then FAILS, its
        episodes go back into the pool so the next nightly pass retries them —
        rather than being lost because they were marked before the agent ran."""
        if not ids:
            return 0
        with self._conn() as c:
            cur = c.executemany(
                "UPDATE episodes SET consolidated_at=NULL WHERE id=?",
                [(i,) for i in ids],
            )
            return cur.rowcount

    def episodes_needing_graph(self, ids: list[int]) -> list[dict]:
        """The subset of `ids` whose facts have not been extracted yet, in id
        order. The graph's own hand-off marker, independent of
        consolidated_at — see the MIGRATIONS note."""
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM episodes WHERE id IN ({marks})"
                " AND graph_extracted_at IS NULL ORDER BY id", ids,
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_episodes_graph_extracted(self, ids: list[int]):
        """Called only after a SUCCESSFUL extraction, so a parse failure or a
        rate-limited run is retried on the next pass rather than dropped."""
        if not ids:
            return
        with self._conn() as c:
            c.executemany(
                "UPDATE episodes SET graph_extracted_at=? WHERE id=?",
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
        # valid_at is a full ISO timestamp, the filters are bare dates:
        # comparing raw strings made before=<today> silently drop today's
        # episodes while keeping today's notes (entries filter on a bare
        # date column and were always inclusive). Compare calendar days.
        if after:
            sql += " AND substr(e.valid_at,1,10) >= ?"
            args.append(after)
        if before:
            sql += " AND substr(e.valid_at,1,10) <= ?"
            args.append(before)
        sql += " ORDER BY score LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            return [dict(r) for r in _fts_with_fallback(c, sql, args, q)]

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

    # -- automations (Phase I) ---------------------------------------------

    def create_automation(self, request: str, source: str, spec: dict,
                          next_run_at: str | None) -> dict:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO automations (created_at, source, request,"
                " task_text, kind, at_time, weekday, interval_minutes,"
                " once_at, next_run_at, notify) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (now(), source, request, spec["task_text"], spec["kind"],
                 spec.get("time"), spec.get("weekday"),
                 spec.get("interval_minutes"), spec.get("once_at"),
                 next_run_at, int(spec.get("notify", True))))
            row = c.execute("SELECT * FROM automations WHERE id=?",
                            (cur.lastrowid,)).fetchone()
            return dict(row)

    def list_automations(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM automations ORDER BY id").fetchall()]

    def get_automation(self, automation_id: int) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM automations WHERE id=?",
                          (automation_id,)).fetchone()
            return dict(r) if r else None

    def due_automations(self, now_iso: str) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM automations WHERE enabled=1"
                " AND next_run_at IS NOT NULL AND next_run_at<=?"
                " ORDER BY next_run_at", (now_iso,)).fetchall()]

    def automation_fired(self, automation_id: int, next_run_at: str | None):
        with self._conn() as c:
            c.execute(
                "UPDATE automations SET last_run_at=?, next_run_at=? WHERE id=?",
                (now(), next_run_at, automation_id))

    def automation_task_started(self, automation_id: int, task_id: str):
        with self._conn() as c:
            c.execute("UPDATE automations SET last_task_id=? WHERE id=?",
                      (task_id, automation_id))

    def set_automation_enabled(self, automation_id: int, enabled: bool,
                               next_run_at: str | None = None) -> bool:
        """Re-enabling passes a freshly computed next_run_at so a long-disabled
        daily doesn't instantly fire on a stale past-due timestamp. And when
        the recompute is None — a spent `once` whose time has passed — the
        NULL is WRITTEN, not skipped: the old code kept the stale timestamp
        and the automation fired within 30s of clicking resume. NULL is the
        spent state everywhere (the scheduler only selects non-NULL rows), so
        a spent once stays spent instead of resurrecting."""
        with self._conn() as c:
            if enabled:
                cur = c.execute(
                    "UPDATE automations SET enabled=1, next_run_at=? WHERE id=?",
                    (next_run_at, automation_id))
            else:
                cur = c.execute("UPDATE automations SET enabled=? WHERE id=?",
                                (int(enabled), automation_id))
            return cur.rowcount > 0

    def delete_automation(self, automation_id: int) -> bool:
        with self._conn() as c:
            cur = c.execute("DELETE FROM automations WHERE id=?", (automation_id,))
            return cur.rowcount > 0

    # -- knowledge graph (Phase J2) ----------------------------------------

    @staticmethod
    def _upsert_entity(c, name: str) -> int:
        """Entity upsert on an existing connection/cursor — lets add_fact do the
        whole fact write (fact + FTS + entities + links) in one transaction so a
        mid-way failure leaves no partial fact."""
        c.execute("INSERT OR IGNORE INTO kg_entities (name, created_at)"
                  " VALUES (?,?)", (name, now()))
        return c.execute("SELECT id FROM kg_entities WHERE name=?",
                         (name,)).fetchone()["id"]

    def upsert_entity(self, name: str) -> int:
        with self._conn() as c:
            return self._upsert_entity(c, name)

    def entity_names(self, limit: int = 200) -> list[str]:
        with self._conn() as c:
            return [r["name"] for r in c.execute(
                "SELECT name FROM kg_entities ORDER BY id LIMIT ?",
                (limit,)).fetchall()]

    def active_fact_id(self, fact: str) -> int | None:
        """Id of an ACTIVE fact with exactly this text, if one exists.

        Deliberately only active: an invalidated fact that later becomes true
        again is a legitimate new row, and resurrecting it would lose the
        bi-temporal history invalidation exists to keep."""
        with self._conn() as c:
            row = c.execute(
                "SELECT id FROM kg_facts WHERE fact=? AND invalid_at IS NULL"
                " ORDER BY id LIMIT 1", (fact,)).fetchone()
            return row["id"] if row else None

    def add_fact(self, fact: str, entity_names: list[str],
                 valid_at: str | None = None,
                 episode_ids: list[int] | None = None) -> int:
        # Exact-text duplicates of a still-ACTIVE fact are rejected, returning
        # the existing id and writing nothing (2026-08-10). The insert used to be
        # unconditional, and the graph has a standing way to see the same
        # episodes twice: /memory/consolidate marks the batch, spawns the
        # consolidation agent AND graph extraction over it, and a FAILED
        # consolidation calls mark_episodes_unconsolidated (the M6 fix) — which
        # returns episodes the extractor has already read. The next nightly pass
        # re-extracts them and, without this, permanently duplicated that night's
        # facts, one copy per retry, none of which the reconcile window may ever
        # reach. Cheap guard: a few hundred rows, no index needed.
        existing = self.active_fact_id(fact)
        if existing is not None:
            log.debug("kg: duplicate active fact, not re-added: %r", fact[:80])
            return existing
        # one transaction: fact + FTS row + entity upserts + links commit or roll
        # back together, so an error partway can't leave a fact with no FTS index
        # or dangling half its entity links (L1).
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO kg_facts (fact, valid_at, episode_ids, created_at)"
                " VALUES (?,?,?,?)",
                (fact, valid_at,
                 json.dumps(episode_ids) if episode_ids else None, now()))
            fid = cur.lastrowid
            c.execute("INSERT INTO kg_facts_fts (fact, fact_id) VALUES (?,?)",
                      (fact, fid))
            for name in entity_names:
                eid = self._upsert_entity(c, name)
                c.execute("INSERT OR IGNORE INTO kg_fact_entities VALUES (?,?)",
                          (fid, eid))
        return fid

    def invalidate_facts(self, ids: list[int],
                         superseded_at: str | None = None) -> int:
        """Retire facts, keeping the bi-temporal columns distinct: invalid_at
        is when the GRAPH learned it (now, record time); expired_at is when
        the fact stopped being true in the WORLD — the superseding
        knowledge's valid_at. The old code stamped both with now(), making
        the columns redundant and losing history the schema says can't be
        retrofitted. And a candidate NEWER than the superseding knowledge is
        never killed by it: the model naming an id is a suggestion, and old
        knowledge must not eat new (skips are logged, not silent)."""
        if not ids:
            return 0
        exp = superseded_at if _is_date(superseded_at) else now()
        with self._conn() as c:
            rows = c.execute(
                f"SELECT id, valid_at FROM kg_facts"
                f" WHERE invalid_at IS NULL AND id IN"
                f" ({','.join('?' * len(ids))})",
                list(ids)).fetchall()
            kill = [r["id"] for r in rows
                    if r["valid_at"] is None or r["valid_at"] <= exp]
            skipped = len(rows) - len(kill)
            if skipped:
                log.info("kg: kept %d candidate(s) newer than the superseding "
                         "knowledge (%s)", skipped, exp)
            if not kill:
                return 0
            cur = c.execute(
                f"UPDATE kg_facts SET invalid_at=?, expired_at=?"
                f" WHERE id IN ({','.join('?' * len(kill))})",
                [now(), exp, *kill])
            return cur.rowcount

    def active_facts(self, limit: int = 80) -> list[dict]:
        """Newest-first active facts. Used for counting and display; the
        invalidation candidate window is candidate_facts()."""
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT id, fact, valid_at FROM kg_facts"
                " WHERE invalid_at IS NULL ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()]

    def candidate_facts(self, limit: int = 80,
                        recent: int | None = None) -> list[dict]:
        """Active facts to offer a model as invalidation candidates.

        Was `ORDER BY id DESC LIMIT ?`, which made every fact outside the newest
        80 (extraction) / 200 (reconcile) IMMORTAL once the graph outgrew that
        window: invalidation is the only removal mechanism ("never deletes"), so
        a contradicted old fact was never shown to either pass again and kept
        ranking in /memory/search?scope=graph forever (2026-08-10).

        Half the window still goes to the newest facts — today's episodes
        usually contradict something recent — and the rest rotates
        oldest-offered-first (NULL = never offered sorts first), so every active
        fact comes up eventually. On the existing db every row starts NULL, so
        the first passes see the OLDEST facts, which is the backlog.

        Must stay DETERMINISTIC between calls: service.py derives the allowed-id
        set and builds the prompt in two separate calls, and if they disagreed
        the model's verdict on a fact it was shown would be silently dropped by
        the anti-wipe guard. Marking is therefore a separate step
        (mark_facts_offered), done at apply time."""
        if limit <= 0:
            return []
        if recent is None:
            recent = max(1, int(limit * CANDIDATE_RECENT_SHARE))
        recent = min(recent, limit)
        with self._conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT id, fact, valid_at FROM kg_facts"
                " WHERE invalid_at IS NULL ORDER BY id DESC LIMIT ?",
                (recent,)).fetchall()]
            have = {r["id"] for r in rows}
            if len(rows) < limit:
                for r in c.execute(
                    "SELECT id, fact, valid_at FROM kg_facts"
                    " WHERE invalid_at IS NULL"
                    " ORDER BY last_offered_at IS NOT NULL, last_offered_at,"
                    "          id LIMIT ?", (limit + recent,)).fetchall():
                    if r["id"] in have:
                        continue
                    rows.append(dict(r))
                    have.add(r["id"])
                    if len(rows) >= limit:
                        break
        return rows

    def mark_facts_offered(self, ids) -> int:
        """Stamp facts as shown to a model, which is what rotates the window in
        candidate_facts. Called on apply, not on selection — see there."""
        ids = list(ids or [])
        if not ids:
            return 0
        ts = now()
        with self._conn() as c:
            cur = c.executemany(
                "UPDATE kg_facts SET last_offered_at=? WHERE id=?",
                [(ts, i) for i in ids])
            return cur.rowcount

    def search_facts(self, q: str, limit: int = 10,
                     include_invalid: bool = False) -> list[dict]:
        match = fts_query_any(q)  # facts are one sentence: OR semantics
        if not match:
            return []
        sql = (
            "SELECT k.id, k.fact, k.valid_at, k.invalid_at,"
            " bm25(kg_facts_fts) AS score,"
            " (SELECT GROUP_CONCAT(e.name, ', ') FROM kg_fact_entities fe"
            "  JOIN kg_entities e ON e.id=fe.entity_id"
            "  WHERE fe.fact_id=k.id) AS entities"
            " FROM kg_facts_fts f JOIN kg_facts k ON k.id = f.fact_id"
            " WHERE kg_facts_fts MATCH ?"
        )
        args: list = [match]
        if not include_invalid:
            sql += " AND k.invalid_at IS NULL"
        sql += " ORDER BY score LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def fact_neighbors(self, fact_ids: list[int], limit: int = 10) -> list[dict]:
        """1-hop expansion: active facts sharing an entity with any of the
        given facts (graphiti BFS collapsed to one join for depth 1)."""
        if not fact_ids:
            return []
        ph = ",".join("?" * len(fact_ids))
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                f"SELECT DISTINCT k.id, k.fact, k.valid_at FROM kg_facts k"
                f" JOIN kg_fact_entities fe ON fe.fact_id=k.id"
                f" WHERE fe.entity_id IN (SELECT entity_id FROM"
                f"  kg_fact_entities WHERE fact_id IN ({ph}))"
                f" AND k.id NOT IN ({ph}) AND k.invalid_at IS NULL"
                f" ORDER BY k.id DESC LIMIT ?",
                [*fact_ids, *fact_ids, limit]).fetchall()]

    def graph_counts(self) -> dict:
        with self._conn() as c:
            return {
                "entities": c.execute(
                    "SELECT COUNT(*) n FROM kg_entities").fetchone()["n"],
                "facts_active": c.execute(
                    "SELECT COUNT(*) n FROM kg_facts WHERE invalid_at IS NULL"
                ).fetchone()["n"],
                "facts_invalidated": c.execute(
                    "SELECT COUNT(*) n FROM kg_facts WHERE invalid_at IS NOT NULL"
                ).fetchone()["n"],
            }

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
                    if self.vec_ok:
                        c.execute("DELETE FROM entries_vec WHERE rowid=?", (row["id"],))
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
                if self.vec_ok:
                    c.execute("DELETE FROM entries_vec WHERE rowid=?", (eid,))
            c.execute("DELETE FROM entries WHERE file_path=?", (path,))
            c.execute("DELETE FROM vault_files WHERE path=?", (path,))
            return len(ids)

    # -- vectors + hybrid search (Phase J1) --------------------------------

    def entries_missing_embeddings(self, limit: int = 128) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT id, compiled FROM entries"
                " WHERE id NOT IN (SELECT rowid FROM entries_vec)"
                " ORDER BY id LIMIT ?", (limit,)).fetchall()]

    def episodes_missing_embeddings(self, limit: int = 128) -> list[dict]:
        with self._conn() as c:
            return [{"id": r["id"],
                     "text": f"{r['user_text']}\n{r['assistant_text'] or ''}"}
                    for r in c.execute(
                        "SELECT id, user_text, assistant_text FROM episodes"
                        " WHERE id NOT IN (SELECT rowid FROM episodes_vec)"
                        " ORDER BY id LIMIT ?", (limit,)).fetchall()]

    @staticmethod
    def _live_ids(c, table: str, ids: list[int]) -> set[int]:
        ph = ",".join("?" * len(ids))
        return {r["id"] for r in c.execute(
            f"SELECT id FROM {table} WHERE id IN ({ph})", ids)}

    def add_entry_embeddings(self, pairs: list[tuple[int, bytes]]):
        if not pairs:
            return
        with self._conn() as c:
            # only embed ids whose entry still exists in THIS transaction: a
            # reindex may have deleted the entry (and its vec row) between the
            # missing-list query and now — an unconditional insert would leave
            # an orphan vector matching nothing (L3).
            live = self._live_ids(c, "entries", [p[0] for p in pairs])
            rows = [p for p in pairs if p[0] in live]
            if rows:
                # OR IGNORE, not OR REPLACE: vec0 (sqlite-vec 0.1.9) RAISES on
                # REPLACE — only UPDATE … WHERE rowid=? works. Two overlapping
                # backfill passes (inbox arrival vs 15-min refresh) compute the
                # same missing list, and the loser's whole executemany aborted
                # on the first colliding rowid, rolling back non-colliding
                # rows too. Measured further: this vec0 build raises even on
                # the OR-IGNORE conflict path, so a residual collision falls
                # back to per-row UPDATE — the one write vec0 honors. Either
                # way the winner's vectors stand; same model, same content.
                # (A model CHANGE re-embeds via reset, which deletes first.)
                try:
                    c.executemany(
                        "INSERT OR IGNORE INTO entries_vec (rowid, embedding) VALUES (?,?)",
                        rows)
                except sqlite3.OperationalError:
                    for rid, emb in rows:
                        c.execute("UPDATE entries_vec SET embedding=? WHERE rowid=?",
                                  (emb, rid))

    def add_episode_embeddings(self, pairs: list[tuple[int, bytes]]):
        if not pairs:
            return
        with self._conn() as c:
            live = self._live_ids(c, "episodes", [p[0] for p in pairs])
            rows = [p for p in pairs if p[0] in live]
            if rows:
                # OR IGNORE + UPDATE fallback — see add_entry_embeddings.
                try:
                    c.executemany(
                        "INSERT OR IGNORE INTO episodes_vec (rowid, embedding) VALUES (?,?)",
                        rows)
                except sqlite3.OperationalError:
                    for rid, emb in rows:
                        c.execute("UPDATE episodes_vec SET embedding=? WHERE rowid=?",
                                  (emb, rid))

    def embedding_identity(self) -> tuple[str | None, int | None]:
        """(model, dim) that wrote the current vectors, (None, None) on a
        fresh db. Vectors are raw model output — same dims, different space
        means wrong neighbors with no error anywhere."""
        try:
            with self._conn() as c:
                rows = dict(c.execute("SELECT k, v FROM embedding_meta").fetchall())
        except sqlite3.OperationalError:
            return None, None
        dim = rows.get("dim")
        return rows.get("model"), int(dim) if dim is not None else None

    def set_embedding_identity(self, model: str, dim: int):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO embedding_meta (k, v) VALUES (?,?)",
                      ("model", model))
            c.execute("INSERT OR REPLACE INTO embedding_meta (k, v) VALUES (?,?)",
                      ("dim", str(dim)))

    def reset_embeddings(self) -> dict:
        """Drop every vector (model change); the next backfill re-embeds from
        source text. Returns what was dropped, for the log."""
        with self._conn() as c:
            out = {}
            if self.vec_ok:
                for table in ("entries_vec", "episodes_vec"):
                    n = c.execute(f"SELECT count(*) n FROM {table}").fetchone()["n"]
                    c.execute(f"DELETE FROM {table}")
                    out[table] = n
        return out

    def sweep_orphan_vectors(self) -> int:
        """Delete vec rows whose backing entry/episode is gone (L3). vec0 tables
        don't take subquery DELETEs, so scan rowids and delete the strays by id.
        Cheap: the vectors number in the hundreds and this runs on the backfill."""
        if not self.vec_ok:
            return 0
        removed = 0
        with self._conn() as c:
            for vtable, src in (("entries_vec", "entries"),
                                ("episodes_vec", "episodes")):
                live = {r["id"] for r in c.execute(f"SELECT id FROM {src}")}
                for r in c.execute(f"SELECT rowid FROM {vtable}").fetchall():
                    if r["rowid"] not in live:
                        c.execute(f"DELETE FROM {vtable} WHERE rowid=?", (r["rowid"],))
                        removed += 1
        return removed

    def _knn(self, c, table: str, qvec: bytes, k: int) -> list[int]:
        return [r["rowid"] for r in c.execute(
            f"SELECT rowid FROM {table} WHERE embedding MATCH ? AND k = ?"
            " ORDER BY distance", (qvec, k)).fetchall()]

    # Fused rank position → row, walked until `limit` LIVE rows are collected.
    # Both hybrid searches used to slice order[:limit] BEFORE fetching the
    # backing rows and then drop ids whose row had vanished, so every orphan
    # vector (a row deleted while vec_ok was False, the L3 window that
    # sweep_orphan_vectors exists to close) silently ate a result slot: the
    # caller asked for 10 and got 8, with nothing logged and the next-best real
    # hits still sitting in `order`. Walk instead of slice (2026-08-10).
    def _fuse(self, c, order, scores, by_id, limit, sql):
        out = []
        for i in order:
            if len(out) >= limit:
                break
            row = by_id.get(i)
            if row is None:
                fetched = c.execute(sql, (i,)).fetchone()
                if not fetched:
                    continue        # orphan vector: skip it, don't spend a slot
                row = dict(fetched)
            out.append({**row, "rrf": round(scores[i], 5)})
        return out

    def search_entries_hybrid(self, q: str, qvec: bytes | None,
                              limit: int = 10) -> list[dict]:
        """FTS BM25 + vector KNN fused with RRF; degrades to FTS-only when
        vectors are unavailable. Same row shape as search_entries + 'rrf'."""
        fts = self.search_entries(q, limit=40)
        if not (self.vec_ok and qvec is not None):
            return fts[:limit]
        with self._conn() as c:
            knn_ids = self._knn(c, "entries_vec", qvec, 40)
            order, scores = _rrf([[r["id"] for r in fts], knn_ids])
            return self._fuse(
                c, order, scores, {r["id"]: dict(r) for r in fts}, limit,
                "SELECT id, file_path, heading, line_no, raw"
                " FROM entries WHERE id=?")

    def search_episodes_hybrid(self, q: str, qvec: bytes | None,
                               limit: int = 10) -> list[dict]:
        fts = self.search_episodes(q, limit=40)
        if not (self.vec_ok and qvec is not None):
            return fts[:limit]
        with self._conn() as c:
            knn_ids = self._knn(c, "episodes_vec", qvec, 40)
            order, scores = _rrf([[r["id"] for r in fts], knn_ids])
            return self._fuse(
                c, order, scores, {r["id"]: dict(r) for r in fts}, limit,
                "SELECT id, task_id, source, kind, area, status,"
                " user_text, assistant_text, valid_at FROM episodes WHERE id=?")

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
            return [dict(r) for r in _fts_with_fallback(c, sql, args, q)]
