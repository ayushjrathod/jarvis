"""FastAPI app: HTTP front door for the dispatcher.

POST /task responds per classification — quick tasks stream as SSE (one round
trip, lowest voice latency); agentic tasks return 202 JSON with a speakable
ack. Content-Type tells the client which path was taken.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import curator, ingest, memory, queue_watcher, vault
from .quick import resolve_backend
from .config import Config
from .service import Service, make_ack

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


class TaskIn(BaseModel):
    text: str
    source: str = "api"
    mode: str = "auto"      # auto | quick | agentic
    area: str | None = None
    metadata: dict | None = None


class LearnIn(BaseModel):
    request: str = ""       # empty: learn from the source's recent conversation
    source: str = "api"


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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        watcher = asyncio.create_task(queue_watcher.watch(svc))
        reindex = (asyncio.create_task(_startup_reindex())
                   if cfg.memory.get("reindex_on_start", True) else None)
        yield
        watcher.cancel()
        if reindex:
            reindex.cancel()
        await svc.shutdown()

    app = FastAPI(title="mission-control dispatcher", lifespan=lifespan)
    app.state.service = svc

    @app.post("/task")
    async def post_task(t: TaskIn):
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
    async def list_tasks(status: str | None = None, kind: str | None = None, limit: int = 50):
        return svc.db.list_tasks(status, kind, limit)

    @app.post("/task/{task_id}/cancel")
    async def cancel(task_id: str):
        ok = await svc.cancel(task_id)
        if not ok:
            raise HTTPException(409, "task missing or already finished")
        return {"task_id": task_id, "status": "cancelled"}

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

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "db": str(cfg.db_path),
            "queue_dir": str(cfg.queue_dir),
            "quick_backend": resolve_backend(cfg),
            "running_tasks": len(svc.bg),
        }

    # -- vault endpoints for the dashboard (mechanical file I/O, no Claude) --

    vault_dir = cfg.root / "vault"

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

    # -- memory endpoints (Phase F) -----------------------------------------

    @app.get("/memory/search")
    async def memory_search(q: str, limit: int = 10, scope: str = "all",
                            file: str | None = None,
                            after: str | None = None,
                            before: str | None = None):
        """FTS5 (BM25) over the vault index and the episode log. `file` is a
        glob-ish path filter (* allowed); after/before hit indexed entry dates
        resp. episode times (ISO)."""
        out: dict = {}
        if scope in ("all", "vault"):
            out["entries"] = svc.db.search_entries(q, limit, file, after, before)
        if scope in ("all", "episodes"):
            out["episodes"] = svc.db.search_episodes(q, limit, after, before)
        if not out:
            raise HTTPException(400, "scope must be all|vault|episodes")
        return out

    @app.post("/memory/reindex")
    async def memory_reindex():
        return await asyncio.to_thread(
            ingest.ingest_vault, svc.db, cfg.root,
            cfg.memory.get("index_dirs", ["vault"]))

    @app.get("/memory/blocks")
    async def memory_blocks():
        return {"context": memory.blocks_context(cfg)}

    @app.post("/memory/consolidate")
    async def memory_consolidate():
        """Export unconsolidated episodes and queue the consolidation agent.
        Episodes are marked at hand-off; the export file in data/consolidation/
        is the audit trail if the run then fails."""
        job = memory.build_consolidation(cfg, svc.db)
        if not job:
            return {"status": "nothing_to_consolidate"}
        svc.db.mark_episodes_consolidated(job["episode_ids"])
        task = await svc.submit(job["text"], source="timer", mode="agentic",
                                area="memory", metadata=job["metadata"])
        return JSONResponse(status_code=202, content={
            "task_id": task["id"], "status": "queued",
            "episodes": len(job["episode_ids"]), "export": job["export_path"],
        })

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
        task = await svc.submit(text, source=l.source, mode="agentic",
                                area="learn", metadata=meta)
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

    # serve the built dashboard, if present (mounted last: API routes win)
    ui_dist = cfg.root / "ui" / "dist"
    if ui_dist.is_dir():
        app.mount("/", StaticFiles(directory=ui_dist, html=True), name="ui")

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
