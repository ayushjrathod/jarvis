"""FastAPI app: HTTP front door for the dispatcher.

POST /task responds per classification — quick tasks stream as SSE (one round
trip, lowest voice latency); agentic tasks return 202 JSON with a speakable
ack. Content-Type tells the client which path was taken.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
from collections.abc import Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import (automations, curator, desktop, embeddings, inbox, ingest, memory,
               queue_watcher, quick, spotify, stt, vault)
from .config import Config
from .db import Database
from .service import Service, make_ack, resolve_screenshot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

_log = logging.getLogger("dispatcher.main")

async def _embed_missing(cfg: Config, db: Database):
    """Best-effort vector backfill, shared by startup, the refresh loop and
    POST /memory/reindex. Hoisted out of create_app with the memory routes."""
    if not embeddings.enabled(cfg):
        return
    try:
        await asyncio.to_thread(embeddings.embed_missing, cfg, db)
    except Exception:
        _log.exception("embedding backfill failed")

# StaticFiles types the PWA manifest by extension. Python 3.14's stdlib already
# maps .webmanifest, but older interpreters don't and would serve it as
# text/plain (Chrome then warns and skips the install prompt) — pin it so the
# content-type is correct regardless of interpreter.
mimetypes.add_type("application/manifest+json", ".webmanifest")

def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"

# /stt takes a raw audio body straight into memory; cap it so a stray/hostile
# upload can't balloon RSS. ~25MB is minutes of Opus — far more than a question.
MAX_STT_BYTES = 25 * 1024 * 1024

# A browser page on another site can POST to http://localhost:8765 (the port is
# guessable); those requests carry a cross-origin Origin header. Local clients
# either send no Origin (curl, the jarvis python client, MediaRecorder to same
# host) or a loopback one (the SPA and the ask-screen popup are served from the
# dispatcher itself). So: reject only a *present, non-local* Origin on
# state-changing methods. Absent Origin is always allowed.
# An ABSENT Origin is allowed by the `if origin` check in the guard below; this
# set is only about a *present* one, so "" must not be a member. It used to be,
# which quietly allowed `Origin: null` — what a sandboxed iframe sends —
# because urlsplit("null").hostname is None. Exploiting it needs a request
# simple enough to skip preflight, which a JSON body isn't, so this is
# defence in depth rather than a closed hole.
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_GUARDED_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

def _origin_is_local(origin: str, extra_hosts: "Iterable[str]" = ()) -> bool:
    try:
        host = urlsplit(origin).hostname or ""
    except ValueError:
        return False
    return host in _LOCAL_HOSTS or host in set(extra_hosts)

class TaskIn(BaseModel):
    text: str
    source: str = "api"
    mode: str = "auto"      # auto | quick | agentic
    area: str | None = None
    metadata: dict | None = None

class LearnIn(BaseModel):
    request: str = ""       # empty: learn from the source's recent conversation
    source: str = "api"

class AutomationIn(BaseModel):
    request: str            # natural language: "every morning, tell me …"
    source: str = "api"

class MediaIn(BaseModel):
    command: str            # "play bohemian rhapsody", "pause", "volume 40"
    source: str = "api"

class DesktopIn(BaseModel):
    command: str            # "lock the screen", "open firefox", "system volume 40"
    source: str = "api"

class DesktopConfirmIn(BaseModel):
    confirm_id: str
    approve: bool = True

def register_vault_routes(app: FastAPI, vault_dir: Path):
    """Dashboard file endpoints (mechanical file I/O, no Claude). First group
    out of create_app's 600-line body; the rest follow group by group."""

    @app.get("/vault/tasks")
    async def vault_tasks():
        return vault.list_tasks(vault_dir)

    @app.post("/vault/tasks/{filename}/toggle")
    async def vault_toggle(filename: str):
        try:
            return vault.toggle_task(vault_dir, filename)
        except FileNotFoundError:
            raise HTTPException(404, "no such task file")
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/vault/brief")
    async def vault_brief():
        brief = vault.current_brief(vault_dir)
        if not brief:
            raise HTTPException(404, "no briefs yet")
        return brief

def register_memory_read_routes(app: FastAPI, svc: Service, cfg: Config):
    """Search, reindex and blocks. The write paths live next door in
    register_memory_write_routes — split because they share serialization
    state, not because they were left behind."""

    @app.get("/memory/search")
    async def memory_search(q: str, limit: int = 10, scope: str = "all",
                            file: str | None = None,
                            after: str | None = None,
                            before: str | None = None):
        """Hybrid FTS5+vector search (RRF-fused) over the vault index and the
        episode log; deterministic filters (file glob, after/before dates)
        force the FTS-only path, khoj-style filters-before-vector. Degrades
        to FTS-only without embeddings/sqlite-vec."""
        qvec = None
        if embeddings.enabled(cfg) and svc.db.vec_ok:
            try:
                qvec = await asyncio.to_thread(embeddings.embed_query, cfg, q)
            except Exception:
                _log.exception("query embedding failed; falling back to FTS")
        out: dict = {}
        if scope in ("all", "vault"):
            out["entries"] = (
                svc.db.search_entries(q, limit, file, after, before)
                if (file or after or before)
                else svc.db.search_entries_hybrid(q, qvec, limit))
        if scope in ("all", "episodes"):
            out["episodes"] = (
                svc.db.search_episodes(q, limit, after, before)
                if (after or before)
                else svc.db.search_episodes_hybrid(q, qvec, limit))
        if scope in ("all", "graph"):
            facts = svc.db.search_facts(q, limit)
            out["facts"] = facts
            out["fact_neighbors"] = svc.db.fact_neighbors(
                [f["id"] for f in facts], limit)
        if not out:
            raise HTTPException(400, "scope must be all|vault|episodes|graph")
        return out

    @app.post("/memory/reindex")
    async def memory_reindex():
        stats = await asyncio.to_thread(
            ingest.ingest_vault, svc.db, cfg.root,
            cfg.memory.get("index_dirs", ["vault"]))
        await _embed_missing(cfg, svc.db)
        return stats

    @app.get("/memory/blocks")
    async def memory_blocks():
        return {"context": memory.blocks_context(cfg)}

def register_memory_write_routes(app: FastAPI, svc: Service, cfg: Config):
    """Consolidate + reconcile. Third group out of create_app; the
    serialization flag lives on app.state, so it rides along."""

    @app.post("/memory/consolidate")
    async def memory_consolidate():
        """Export unconsolidated episodes and queue the consolidation agent.
        Episodes are marked at hand-off; the export file in data/consolidation/
        is the audit trail if the run then fails.

        Serialized (2026-08-10): build-then-mark is a read-then-write with an
        await in between, so two callers — the 02:30 timer and a manual POST,
        or a double-fire — could both read the SAME unconsolidated set before
        either marked it, and both would submit an agent run over it. Export
        filenames no longer collide, but the duplicate *run* is the expensive
        half. asyncio has no preemption, so a plain flag is a sufficient lock
        here; a second caller is told nothing to do rather than made to wait,
        because the first one already claimed every episode there was.
        """
        if getattr(app.state, "consolidating", False):
            return {"status": "already_running"}
        app.state.consolidating = True
        try:
            return await _consolidate()
        finally:
            app.state.consolidating = False

    async def _consolidate():
        job = memory.build_consolidation(cfg, svc.db)
        if not job:
            return {"status": "nothing_to_consolidate"}
        svc.db.mark_episodes_consolidated(job["episode_ids"])
        # carry episode_ids so a FAILED run can put them back in the pool (M6)
        meta = {**job["metadata"], "episode_ids": job["episode_ids"]}
        task = await svc.submit(job["text"], source="timer", mode="agentic",
                                area="memory", metadata=meta,
                                trusted=True)  # server spawn (consolidation agent)
        # Graph extraction (Phase J2) runs in parallel with the
        # block-consolidation agent, but over its OWN subset (2026-08-11): the
        # graph marks episodes only on success, and a failed consolidation
        # rolls `consolidated_at` back, so the next pass re-exports episodes
        # the extractor already digested. Without this filter those facts are
        # re-derived every retry — survivable only because `add_fact` rejects
        # exact duplicates of an active fact, which is a guard, not a design.
        try:
            pending = svc.db.episodes_needing_graph(job["episode_ids"])
            if pending:
                svc.spawn_graph_extract(memory.render_episodes(pending),
                                        [e["id"] for e in pending])
            else:
                _log.info("graph extract: nothing new in this batch")
        except Exception:
            _log.exception("graph extract spawn failed")
        return JSONResponse(status_code=202, content={
            "task_id": task["id"], "status": "queued",
            "episodes": len(job["episode_ids"]), "export": job["export_path"],
        })

    @app.post("/memory/reconcile")
    async def memory_reconcile():
        """Weekly knowledge-graph reconciliation (mem0 ADD/UPDATE/DELETE
        spirit): duplicates and contradicted facts get invalidated."""
        if not svc.graph_enabled():
            return {"status": "graph_disabled"}
        return await svc.graph_reconcile()

def register_automation_routes(app: FastAPI, svc: Service, cfg: Config):
    """Standing automations. Fourth group out of create_app."""
    # -- automation endpoints (Phase I) -------------------------------------

    def _automation_view(row: dict) -> dict:
        return {**row, "describe": automations.describe(automations.spec_from_row(row))}

    @app.post("/automations")
    async def create_automation(a: AutomationIn):
        """NL request → one LLM parse → validated standing automation."""
        if not a.request.strip():
            raise HTTPException(400, "empty request")
        # The scheduler only polls when the automations block is enabled; the
        # old code returned 201 "Scheduled" over a row nothing would ever run.
        if not (svc.cfg.automations or {}).get("enabled"):
            raise HTTPException(409, "automations are disabled in config.yaml "
                                     "(add an `automations:` block) — nothing "
                                     "would run this row")
        row, speech = await svc.create_automation_from_nl(a.request, a.source)
        if not row:
            raise HTTPException(422, speech)
        return JSONResponse(status_code=201,
                            content={**_automation_view(row), "speech": speech})

    @app.get("/automations")
    async def list_automations():
        return [_automation_view(r) for r in svc.db.list_automations()]

    @app.post("/automations/{automation_id}/toggle")
    async def toggle_automation(automation_id: int):
        row = svc.db.get_automation(automation_id)
        if not row:
            raise HTTPException(404, "no such automation")
        enabling = not row["enabled"]
        # recompute on re-enable: a stale past-due next_run_at must not fire
        next_at = (automations.next_run_iso(automations.spec_from_row(row))
                   if enabling else None)
        svc.db.set_automation_enabled(automation_id, enabling, next_at)
        return _automation_view(svc.db.get_automation(automation_id))

    @app.delete("/automations/{automation_id}")
    async def delete_automation(automation_id: int):
        if not svc.db.delete_automation(automation_id):
            raise HTTPException(404, "no such automation")
        return {"automation_id": automation_id, "status": "deleted"}

def _spa_index(root: Path) -> FileResponse:
    """Serve the SPA shell for a non-'/' page. The StaticFiles mount below
    only falls back to index.html for directories, so every client-routed
    path needs its own route."""
    index = root / "ui" / "dist" / "index.html"
    if not index.is_file():
        raise HTTPException(404, "ui not built")
    # Same no-cache as the middleware gives "/": these URLs serve the shell.
    return FileResponse(index, media_type="text/html",
                        headers={"Cache-Control": "no-cache"})

def register_screen_routes(app: FastAPI, cfg: Config):
    """Ask-about-my-screen: STT upload, screenshot serving, popup shell."""
    # -- ask-about-my-screen ------------------------------------------------

    @app.post("/stt")
    async def transcribe(request: Request):
        """Raw-body audio upload (webm/opus from MediaRecorder, or wav) →
        transcript. Raw body on purpose: python-multipart isn't a dep. Read
        as a stream with a running total: the old code collected every chunk
        first and THEN checked the size, so the cap bounded nothing on the
        chunked path it claimed to — a lying Content-Length meant unbounded
        memory before the 413."""
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_STT_BYTES:
            raise HTTPException(413, "audio too large")
        chunks, total = [], 0
        async for piece in request.stream():
            total += len(piece)
            if total > MAX_STT_BYTES:
                raise HTTPException(413, "audio too large")
            chunks.append(piece)
        data = b"".join(chunks)
        if len(data) < 100:
            raise HTTPException(400, "no audio")
        try:
            text = await asyncio.to_thread(stt.get_stt(cfg).transcribe_bytes, data)
        except Exception as e:
            raise HTTPException(422, f"could not decode audio: {e}")
        return {"text": text}

    @app.get("/screenshots/{name}")
    async def screenshot(name: str):
        p = resolve_screenshot(cfg, name)
        if not p:
            raise HTTPException(404, "no such screenshot")
        return FileResponse(p, media_type="image/png")

    @app.get("/ask")
    async def ask_page():
        return _spa_index(cfg.root)

def register_media_routes(app: FastAPI, svc: Service, cfg: Config):
    """Spotify transport + search, direct for UI and scripts."""
    # -- media endpoints ----------------------------------------------------

    def _require_media():
        if not (cfg.media or {}).get("enabled"):
            raise HTTPException(503, "media control is disabled in config.yaml")

    @app.post("/media")
    async def media_command(m: MediaIn):
        """Same handler the POST /task divert uses, for the UI and scripts."""
        _require_media()
        if not m.command.strip():
            raise HTTPException(400, "empty command")
        out = await svc.media_command(m.command, m.source)
        return {"speech": out["speech"], "status": out["status"]}

    @app.get("/media/state")
    async def media_state():
        _require_media()
        return await asyncio.to_thread(spotify.state, cfg)

def register_desktop_routes(app: FastAPI, svc: Service, cfg: Config):
    """Computer-use T1 verbs, confirm plane included."""
    # -- desktop control (computer-use T1) ----------------------------------

    def _require_computer():
        if not (cfg.computer or {}).get("enabled"):
            raise HTTPException(503, "desktop control is disabled in config.yaml")

    @app.post("/desktop")
    async def desktop_command(d: DesktopIn):
        """Same handler the POST /task divert uses, for the UI and scripts.
        A verb whose policy is 'confirm' returns needs_confirmation + a
        confirm_id rather than acting; answer it at /desktop/confirm."""
        _require_computer()
        if not d.command.strip():
            raise HTTPException(400, "empty command")
        return await svc.desktop_command(d.command, d.source)

    @app.post("/desktop/confirm")
    async def desktop_confirm(c: DesktopConfirmIn):
        _require_computer()
        return await svc.confirm_desktop(c.confirm_id, c.approve)

    @app.get("/desktop/verbs")
    async def desktop_verbs():
        """What this tier can do and under what policy — the honest surface
        for the dashboard and for anyone wondering why a verb refused."""
        _require_computer()
        return {"verbs": {v: desktop.policy(cfg.computer, v)
                          for v in sorted(desktop.ALL_VERBS)},
                "pending": len(svc.pending_desktop)}

def register_learning_routes(app: FastAPI, svc: Service, cfg: Config):
    """Skill authoring, usage, lifecycle curation."""
    # -- learning endpoints (Phase G) ---------------------------------------

    @app.post("/learn")
    async def learn(l: LearnIn):
        """Author an area skill from a described workflow; with an empty
        request, distill the source's recent quick conversation instead
        (rides the same session-resume machinery as voice follow-ups)."""
        resume = svc._fresh_quick_session(l.source)
        text = l.request.strip() or (
            "Distill the workflow from our conversation above into an area skill.")
        if not l.request.strip() and not resume:
            raise HTTPException(400, "empty request and no recent conversation to learn from")
        meta = {"task_type": "learn"}
        if resume:
            meta["resume_session_id"] = resume
        # server-constructed metadata (resume id is computed here, not caller-
        # supplied): trusted so the learn-from-conversation resume survives
        task = await svc.submit(text, source=l.source, mode="agentic",
                                area="learn", metadata=meta, trusted=True)
        return JSONResponse(status_code=202, content={
            "task_id": task["id"], "status": "queued", "resumed": bool(resume)})

    @app.get("/skills")
    async def skills():
        usage = {u["name"]: u for u in svc.db.skill_usage_all()}
        out = []
        for name, area in sorted(svc.areas.load().items()):
            row = usage.get(name) or {}
            out.append({
                "name": name,
                "triggers": area.triggers, "quick_triggers": area.quick_triggers,
                "use_count": row.get("use_count", 0),
                "patch_count": row.get("patch_count", 0),
                "last_used_at": row.get("last_used_at"),
                "last_patched_at": row.get("last_patched_at"),
                "state": row.get("state", "active"),
                "pinned": bool(row.get("pinned", 0)),
            })
        return out

    @app.post("/skills/curate")
    async def skills_curate():
        report = await asyncio.to_thread(curator.run, svc.db, cfg)
        await svc.hooks.fire({"event": "curated", **report})
        return report

def register_task_routes(app: FastAPI, svc: Service):
    """Submit, fetch, list, cancel. The dispatch core stays addressable."""
    @app.post("/task")
    async def post_task(t: TaskIn):
        # Deterministic diverts (standing automation → media → desktop) live on
        # the service so the queue watcher and the automations scheduler get
        # them too; mode=auto only, so an explicit quick/agentic bypasses and
        # nothing is unreachable. Streamed here rather than recorded as a task:
        # over HTTP the caller is waiting on the answer, and none of these is a
        # cancellable task row.
        if t.mode == "auto":
            divert = await svc.try_divert(t.text, t.source)
            if divert is not None:

                async def diverted():
                    yield sse("task", {"task_id": None, "kind": divert["kind"]})
                    yield sse("delta", {"text": divert["speech"]})
                    # status comes from the EXECUTOR, not a hardcoded "done"
                    # (fixed 2026-08-10 — the two halves of one feature
                    # disagreed: an unresolved "put on something chill"
                    # streamed status=done here while the identical command
                    # through submit() settled 'failed', and an SSE client had
                    # no field to tell a failed play from a real one).
                    #
                    # ...with one deliberate difference from _settle_divert: a
                    # parked confirmation is NOT a failure over HTTP. There the
                    # caller is present, gets `confirm_id`, and answers via the
                    # ConfirmBar or by voice — the interaction is proceeding
                    # normally. It settles 'failed' on the queue/automation
                    # path only because nobody is watching those sources, so
                    # the intent just expires. Reporting 'failed' here would
                    # make the dashboard show an error for an ordinary
                    # "Shall I read your clipboard?" prompt.
                    acted = divert.get("ok") or divert.get("confirm_id")
                    yield sse("done", {k: v for k, v in {
                        "status": "done" if acted else "failed",
                        "kind": divert["kind"],
                        "automation_id": divert.get("automation_id"),
                        "desktop_status": divert.get("desktop_status"),
                        "confirm_id": divert.get("confirm_id"),
                    }.items() if v is not None or k == "status"})

                return StreamingResponse(diverted(), media_type="text/event-stream")

        kind, area = svc.route(t.text, t.mode, t.area)
        if kind == "agentic":
            task = await svc.submit(t.text, t.source, "agentic", area, t.metadata)
            return JSONResponse(status_code=202, content={
                "task_id": task["id"], "kind": "agentic",
                "status": "queued", "ack": make_ack(t.text),
            })
        task = await svc.create_task(t.text, t.source, "quick", area, t.metadata)

        async def gen():
            async for event, payload in svc.stream_quick(task):
                yield sse(event, payload)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/task/{task_id}")
    async def get_task(task_id: str):
        task = svc.db.get_task(task_id)
        if not task:
            raise HTTPException(404, "no such task")
        return task

    @app.get("/tasks")
    async def list_tasks(status: str | None = None, kind: str | None = None,
                         limit: int = 50, source: str | None = None):
        return svc.db.list_tasks(status, kind, limit, source)

    @app.post("/task/{task_id}/cancel")
    async def cancel(task_id: str):
        ok = await svc.cancel(task_id)
        if not ok:
            raise HTTPException(409, "task missing or already finished")
        return {"task_id": task_id, "status": "cancelled"}

def register_obs_routes(app: FastAPI, svc: Service, cfg: Config):
    """Events, stats, step timelines, health."""
    @app.get("/events")
    async def events():
        async def gen():
            q = svc.bus.subscribe()
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=25)
                        yield sse(ev.get("event", "update"), ev)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            finally:
                svc.bus.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/stats")
    async def stats(days: int = 7):
        """Observability aggregates (Phase H): task counts, success rate,
        cost, quick-path latency, recent reflections."""
        return svc.db.stats_summary(days)

    @app.get("/task/{task_id}/steps")
    async def task_steps(task_id: str):
        task = svc.db.get_task(task_id)
        if not task:
            raise HTTPException(404, "no such task")
        return {r["id"]: svc.db.get_run_steps(r["id"]) for r in task["runs"]}

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "db": str(cfg.db_path),
            "queue_dir": str(cfg.queue_dir),
            "quick_backend": quick.BACKEND,
            "running_tasks": len(svc.bg),
        }

    @app.get("/debug/host")
    async def debug_host(request: Request):
        """Report the Host (and Origin) headers as this server sees them.

        Exists for exactly one job: the Host-header check the origin guard
        needs — open this URL from the phone over Tailscale Serve and paste
        the `host` value into security.public_hosts. Echoes only what the
        caller's own request carried; no server internals.
        """
        return {
            "host": request.headers.get("host"),
            "origin": request.headers.get("origin"),
        }

def register_docs_routes(app: FastAPI, cfg: Config):
    """System docs page plus the static SPA mount (last: API wins)."""
    # -- docs page ----------------------------------------------------------

    @app.get("/system-docs")
    async def docs_page():
        """Human docs (guide + API reference). NOT /docs — that's FastAPI's
        Swagger UI, which stays where it is."""
        return _spa_index(cfg.root)

    # serve the built dashboard, if present (mounted last: API routes win)
    ui_dist = cfg.root / "ui" / "dist"
    if ui_dist.is_dir():
        app.mount("/", StaticFiles(directory=ui_dist, html=True), name="ui")

def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or Config.load()
    svc = Service(cfg)

    log = logging.getLogger("dispatcher.main")

    async def _startup_reindex():
        try:
            stats = await asyncio.to_thread(
                ingest.ingest_vault, svc.db, cfg.root,
                cfg.memory.get("index_dirs", ["vault"]))
            log.info("vault reindex: %s", stats)
        except Exception:
            log.exception("startup vault reindex failed")
        await _embed_missing(cfg, svc.db)

    async def _embed_refresh_loop():
        interval = max(1, (cfg.embeddings or {}).get("refresh_minutes", 15)) * 60
        while True:
            await asyncio.sleep(interval)
            await _embed_missing(cfg, svc.db)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        watcher = asyncio.create_task(queue_watcher.watch(svc))
        reindex = (asyncio.create_task(_startup_reindex())
                   if cfg.memory.get("reindex_on_start", True) else None)
        scheduler = (asyncio.create_task(automations.loop(svc))
                     if (cfg.automations or {}).get("enabled") else None)
        embedder = (asyncio.create_task(_embed_refresh_loop())
                    if embeddings.enabled(cfg) else None)
        inbox_watcher = (asyncio.create_task(inbox.watch(svc))
                         if (cfg.inbox or {}).get("enabled") else None)
        yield
        for t in (watcher, reindex, scheduler, embedder, inbox_watcher):
            if t:
                t.cancel()
        await svc.shutdown()

    app = FastAPI(title="mission-control dispatcher", lifespan=lifespan)
    app.state.service = svc

    @app.middleware("http")
    async def guard_origin(request: Request, call_next):
        """Lightweight CSRF guard (M1): a state-changing request from a browser
        page on some other origin is rejected; local clients (no Origin, or a
        loopback/self Origin) pass through untouched."""
        if request.method in _GUARDED_METHODS:
            origin = request.headers.get("origin")
            if origin and not _origin_is_local(origin, (cfg.host, *cfg.public_hosts)):
                return JSONResponse(status_code=403,
                                    content={"detail": "cross-origin request rejected"})
        return await call_next(request)

    @app.middleware("http")
    async def cache_ui(request: Request, call_next):
        """Cache headers the static mount doesn't set. Starlette sends only
        ETag + Last-Modified, and heuristic freshness (10% of file age)
        served a 30-day-old index.html with zero requests — the phone PWA
        launching by navigation is exactly the victim, and the service worker
        then guarantees the matching old bundle. Hashed /assets/* are
        immutable by construction (Vite); the shell itself is always
        revalidated. API and SSE responses pass through untouched."""
        resp = await call_next(request)
        if request.method == "GET" and resp.status_code == 200:
            path = request.url.path
            if path.startswith("/assets/"):
                resp.headers["Cache-Control"] = (
                    "public, max-age=31536000, immutable")
            elif path == "/" or path.endswith(".html"):
                resp.headers["Cache-Control"] = "no-cache"
            elif path in ("/sw.js", "/manifest.webmanifest",
                          "/favicon.ico", "/favicon.png",
                          "/apple-touch-icon.png") or path.startswith("/icon-"):
                # Small, versioned-rarely, correctness-critical: a stale sw.js
                # pins the old shell offline, and stale icons/manifest break
                # the installed PWA's identity. Always revalidate.
                resp.headers["Cache-Control"] = "no-cache"
        return resp

    register_task_routes(app, svc)

    register_obs_routes(app, svc, cfg)

    # -- vault endpoints for the dashboard (mechanical file I/O, no Claude) --
    register_vault_routes(app, cfg.root / "vault")
    register_memory_read_routes(app, svc, cfg)

    register_memory_write_routes(app, svc, cfg)

    register_screen_routes(app, cfg)

    register_automation_routes(app, svc, cfg)

    register_media_routes(app, svc, cfg)

    register_desktop_routes(app, svc, cfg)

    register_learning_routes(app, svc, cfg)

    register_docs_routes(app, cfg)

    return app

def main():
    cfg = Config.load()
    # graceful-shutdown cap: the always-open /events SSE streams otherwise hold
    # shutdown until systemd's stop timeout SIGKILLs us — which is what used to
    # orphan 'running' task rows on every restart
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="info",
                timeout_graceful_shutdown=5)

if __name__ == "__main__":
    main()
