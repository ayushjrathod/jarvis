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

from . import queue_watcher, vault
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


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or Config.load()
    svc = Service(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        watcher = asyncio.create_task(queue_watcher.watch(svc))
        yield
        watcher.cancel()
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

    # serve the built dashboard, if present (mounted last: API routes win)
    ui_dist = cfg.root / "ui" / "dist"
    if ui_dist.is_dir():
        app.mount("/", StaticFiles(directory=ui_dist, html=True), name="ui")

    return app


def main():
    cfg = Config.load()
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
