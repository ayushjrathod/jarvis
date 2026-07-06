"""Service layer: the single dispatch path (locked decision #1). Every task —
voice, UI, queue, timer — flows through here; nothing else invokes Claude.
"""

from __future__ import annotations

import asyncio
import json
import logging

from . import quick, runner
from .areas import AreaRegistry
from .classifier import classify
from .config import Config
from .db import Database
from .events import EventBus
from .hooks import HookRegistry

log = logging.getLogger("dispatcher.service")

TERMINAL = {"done", "failed", "cancelled"}


def make_ack(text: str) -> str:
    words = text.split()
    short = " ".join(words[:8])
    return f"On it — {short}{'…' if len(words) > 8 else ''}"


class Service:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = Database(cfg.db_path)
        self.bus = EventBus()
        self.hooks = HookRegistry()
        self.hooks.register(self.bus.publish)  # SSE broadcaster is the first hook
        self.sem = asyncio.Semaphore(cfg.max_concurrent_agentic)
        self.procs: dict = {}      # task_id -> subprocess (for cancel/barge-in)
        self.bg: dict = {}         # task_id -> asyncio.Task
        self.areas = AreaRegistry(cfg.root / "areas")

    async def fire(self, event: str, task: dict, **extra):
        payload = {
            "event": event,
            "task_id": task["id"],
            "kind": task["kind"],
            "source": task["source"],
            "text": task["text"][:200],
            **extra,
        }
        await self.hooks.fire(payload)

    def route(self, text: str, mode: str, area: str | None = None) -> tuple[str, str | None]:
        """(kind, area). Area quick_triggers beat triggers beat the classifier;
        an explicit mode always wins the kind, an explicit area wins the area."""
        matched, hint = self.areas.match(text)
        area_name = area or (matched.name if matched else None)
        if mode in ("quick", "agentic"):
            return mode, area_name
        return (hint or classify(text)), area_name

    async def create_task(self, text, source, kind, area=None, metadata=None) -> dict:
        task = self.db.create_task(text, source, kind, area, metadata)
        await self.fire("queued", task)
        return task

    async def submit(self, text, source="api", mode="auto", area=None, metadata=None) -> dict:
        """Fire-and-forget entry point (queue watcher, timers). Quick tasks run
        in the background with output stored in the run row; HTTP clients that
        want streamed quick answers go through create_task + stream_quick."""
        kind, area_name = self.route(text, mode, area)
        task = await self.create_task(text, source, kind, area_name, metadata)
        if kind == "agentic":
            self.start_agentic(task)
        else:
            self.bg[task["id"]] = asyncio.create_task(self._drain_quick(task))
        return task

    # -- quick path ---------------------------------------------------------

    async def stream_quick(self, task: dict):
        """Async generator of (event_name, payload) pairs; writes run rows and
        retries once on the fallback model when the backend asks for it."""
        self.db.set_task_status(task["id"], "running")
        await self.fire("started", task)
        yield ("task", {"task_id": task["id"], "kind": "quick"})

        area = self.areas.get(task["area"]) if task.get("area") else None
        q_tools = area.quick_allowed_tools if area else None
        q_context = area.context() if area else ""

        models = [None, self.cfg.models.get("fallback", "claude-opus-4-8")]
        for attempt, model_override in enumerate(models, start=1):
            label = model_override or self.cfg.models.get("quick") or "cli-default"
            run_id = self.db.create_run(task["id"], attempt, label)
            meta, collected = {}, []
            try:
                async for kind, payload in quick.stream(
                    task["text"], self.cfg, model_override,
                    tools=q_tools, context=q_context,
                ):
                    if kind == "delta":
                        collected.append(payload)
                        yield ("delta", {"text": payload})
                    else:
                        meta = payload
            except Exception as e:
                log.exception("quick run failed for %s", task["id"])
                meta = {"status": "failed", "error": f"{type(e).__name__}: {e}"}

            status = meta.get("status", "failed")
            self.db.finish_run(
                run_id, status,
                stop_reason=meta.get("stop_reason"), cost_usd=meta.get("cost_usd"),
                input_tokens=meta.get("input_tokens"), output_tokens=meta.get("output_tokens"),
                session_id=meta.get("session_id"), error=meta.get("error"),
                output_text="".join(collected) or None,
            )
            if status == "refused" and meta.get("retry_on_refusal") and attempt == 1:
                await self.fire("refused", task, attempt=attempt, model=label)
                continue

            final = "done" if status == "done" else "failed"
            self.db.set_task_status(task["id"], final)
            await self.fire(final, task, cost_usd=meta.get("cost_usd"), model=label)
            yield ("done", {
                "task_id": task["id"], "status": final,
                "cost_usd": meta.get("cost_usd"), "model": meta.get("model", label),
                "error": meta.get("error"),
            })
            return

    async def _drain_quick(self, task: dict):
        """Run a quick task with no streaming client; the run row keeps the answer."""
        try:
            async for _event, _payload in self.stream_quick(task):
                pass
        finally:
            self.bg.pop(task["id"], None)

    # -- agentic path ---------------------------------------------------------

    def start_agentic(self, task: dict):
        self.bg[task["id"]] = asyncio.create_task(self._run_agentic(task))

    async def _run_agentic(self, task: dict):
        try:
            async with self.sem:
                await self._run_agentic_inner(task)
        except asyncio.CancelledError:
            pass  # cancel() already wrote state and fired the event
        except Exception:
            log.exception("agentic task %s crashed", task["id"])
            self.db.set_task_status(task["id"], "failed")
            await self.fire("failed", task, error="internal error")
        finally:
            self.bg.pop(task["id"], None)

    async def _run_agentic_inner(self, task: dict):
        self.db.set_task_status(task["id"], "running")
        await self.fire("started", task)
        meta = json.loads(task["metadata"]) if task.get("metadata") else {}
        area = self.areas.get(task["area"]) if task.get("area") else None
        tools = (
            meta.get("allowed_tools")                                # agent-file override
            or (area.allowed_tools if area and area.allowed_tools else None)
            or self.cfg.tools_for(meta.get("task_type"))
        )
        system_extra = area.context() if area else ""
        attempts = [
            (1, self.cfg.models.get("agentic")),
            (2, self.cfg.models.get("fallback", "claude-opus-4-8")),
        ]
        simulate = bool(meta.get("simulate_refusal"))

        for attempt, model in attempts:
            label = model or "cli-default"
            run_id = self.db.create_run(task["id"], attempt, label)
            if simulate and attempt == 1:
                result = {"status": "refused", "stop_reason": "refusal",
                          "error": "simulated refusal (metadata.simulate_refusal)"}
            else:
                result = await runner.run_once(
                    task["text"], self.cfg, model, tools, self.procs, task["id"],
                    system_extra=system_extra,
                )
            output_path = runner.guess_output_path(task["text"], self.cfg.root)
            self.db.finish_run(
                run_id, result["status"],
                stop_reason=result.get("stop_reason"), cost_usd=result.get("cost_usd"),
                input_tokens=result.get("input_tokens"), output_tokens=result.get("output_tokens"),
                num_turns=result.get("num_turns"), session_id=result.get("session_id"),
                output_text=result.get("output_text"), error=result.get("error"),
                output_path=output_path,
            )
            if result["status"] == "refused" and attempt == 1:
                log.warning("task %s refused on %s; retrying on fallback", task["id"], label)
                await self.fire("refused", task, attempt=attempt, model=label)
                continue

            final = "done" if result["status"] == "done" else "failed"
            self.db.set_task_status(task["id"], final)
            await self.fire(final, task, cost_usd=result.get("cost_usd"),
                            model=label, output_path=output_path,
                            error=result.get("error"))
            return

    # -- cancel / shutdown ----------------------------------------------------

    async def cancel(self, task_id: str) -> bool:
        task = self.db.get_task(task_id)
        if not task or task["status"] in TERMINAL:
            return False
        proc = self.procs.pop(task_id, None)
        if proc and proc.returncode is None:
            proc.kill()
        bg = self.bg.pop(task_id, None)
        if bg:
            bg.cancel()
        self.db.cancel_open_runs(task_id)
        self.db.set_task_status(task_id, "cancelled")
        await self.fire("cancelled", task)
        return True

    async def shutdown(self):
        for task_id, bg in list(self.bg.items()):
            bg.cancel()
        for proc in self.procs.values():
            if proc.returncode is None:
                proc.kill()
