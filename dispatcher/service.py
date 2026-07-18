"""Service layer: the single dispatch path (locked decision #1). Every task —
voice, UI, queue, timer — flows through here; nothing else invokes Claude.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

from . import limits, memory, quick, reflection, runner
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


def is_resume_error(error: str | None) -> bool:
    """True when a failure is the resumed session having vanished (CLI session
    GC / config wipe): 'No conversation found with session ID: …'. The right
    reaction is a fresh-session retry, not a user-facing failure."""
    return "no conversation found" in (error or "").lower()


class Service:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = Database(cfg.db_path)
        orphans = self.db.reconcile_orphans()
        if orphans:
            log.warning("marked %d orphaned task(s) from a previous run as failed", orphans)
        self.bus = EventBus()
        self.hooks = HookRegistry()
        self.hooks.register(self.bus.publish)  # SSE broadcaster is the first hook
        self.sem = asyncio.Semaphore(cfg.max_concurrent_agentic)
        self.procs: dict = {}      # task_id -> subprocess (for cancel/barge-in)
        self.bg: dict = {}         # task_id -> asyncio.Task
        self.areas = AreaRegistry(cfg.root / "areas")
        # quick-path continuity: source -> (last CLI session_id, monotonic time
        # of last completed turn). In-memory on purpose — the idle window is
        # minutes, a restart just means one fresh start.
        self.quick_sessions: dict[str, tuple[str, float]] = {}

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

    def _with_memory(self, context: str) -> str:
        """Prepend the always-in-context memory blocks (Phase F) to a run's
        extra system context. A broken block file must never take down the
        dispatch path — worst case the run just goes out memoryless."""
        try:
            mem = memory.blocks_context(self.cfg)
        except Exception:
            log.exception("memory blocks failed to load")
            mem = ""
        return "\n\n".join(x for x in (mem, context) if x)

    def _capture_episode(self, task: dict, status: str, answer: str | None):
        """Append the settled interaction to the episodes table (Phase F).
        Raw capture is free (no LLM); the nightly consolidation agent distills
        it. Best-effort by design — capture failure never fails the task."""
        if not memory.should_capture(self.cfg, task, status):
            return
        try:
            self.db.add_episode(task["id"], task["source"], task["kind"],
                                task.get("area"), status, task["text"], answer)
        except Exception:
            log.exception("episode capture failed for %s", task["id"])

    def route(self, text: str, mode: str, area: str | None = None,
              match_area: bool = True) -> tuple[str, str | None]:
        """(kind, area). Area quick_triggers beat triggers beat the classifier;
        an explicit mode always wins the kind, an explicit area wins the area.
        match_area=False skips trigger matching entirely — meta-tasks like
        reflection would otherwise match areas on their own prompt text."""
        if not match_area:
            kind = mode if mode in ("quick", "agentic") else classify(text)
            return kind, area
        matched, hint = self.areas.match(text)
        area_name = area or (matched.name if matched else None)
        if mode in ("quick", "agentic"):
            return mode, area_name
        return (hint or classify(text)), area_name

    async def create_task(self, text, source, kind, area=None, metadata=None) -> dict:
        task = self.db.create_task(text, source, kind, area, metadata)
        if area:  # skill telemetry (Phase G) — best-effort, never blocks dispatch
            try:
                self.db.record_skill_use(area)
            except Exception:
                log.exception("skill-use bump failed for %s", area)
        await self.fire("queued", task)
        return task

    async def submit(self, text, source="api", mode="auto", area=None,
                     metadata=None, match_area=True) -> dict:
        """Fire-and-forget entry point (queue watcher, timers). Quick tasks run
        in the background with output stored in the run row; HTTP clients that
        want streamed quick answers go through create_task + stream_quick."""
        kind, area_name = self.route(text, mode, area, match_area)
        task = await self.create_task(text, source, kind, area_name, metadata)
        if kind == "agentic":
            self.start_agentic(task)
        else:
            self.bg[task["id"]] = asyncio.create_task(self._drain_quick(task))
        return task

    # -- quick path ---------------------------------------------------------

    def _fresh_quick_session(self, source: str) -> str | None:
        """Session to resume for a follow-up turn, or None for a fresh start.
        Idle-freshness per openclaw reset-policy.ts: the source's last session
        holds until quick_session_idle_minutes pass without a completed turn
        (0 = continuity off)."""
        idle_min = self.cfg.quick_session_idle_minutes
        entry = self.quick_sessions.get(source)
        if not idle_min or not entry:
            return None
        session_id, last_used = entry
        if time.monotonic() - last_used > idle_min * 60:
            self.quick_sessions.pop(source, None)
            return None
        return session_id

    async def stream_quick(self, task: dict):
        """Async generator of (event_name, payload) pairs; writes run rows and
        retries once on the fallback model when the backend asks for it.
        Follow-ups within the idle window resume the source's previous CLI
        session (`claude -p --resume`) so short back-and-forths keep context.

        The finally block settles the books when the client vanishes mid-stream
        (barge-in closes the connection, browser tab dies): GeneratorExit /
        CancelledError land at a yield, so without it the task and run rows
        would sit 'running' forever. DB calls only — no awaits are safe there.
        """
        self.db.set_task_status(task["id"], "running")
        await self.fire("started", task)
        finished = False
        open_run = None
        try:
            yield ("task", {"task_id": task["id"], "kind": "quick"})

            area = self.areas.get(task["area"]) if task.get("area") else None
            q_tools = area.quick_allowed_tools if area else None
            q_context = self._with_memory(area.context() if area else "")

            models = [None, self.cfg.models.get("fallback", "claude-opus-4-8")]
            resume_id = self._fresh_quick_session(task["source"])
            attempt, idx = 0, 0
            while idx < len(models):
                model_override = models[idx]
                attempt += 1
                label = model_override or self.cfg.models.get("quick") or "cli-default"
                run_id = self.db.create_run(task["id"], attempt, label)
                open_run = run_id
                meta, collected = {}, []
                try:
                    async for kind, payload in quick.stream(
                        task["text"], self.cfg, model_override,
                        tools=q_tools, context=q_context,
                        resume_session_id=resume_id,
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
                open_run = None
                limit = (limits.classify_limit(meta.get("error"))
                         if status == "failed" else None)
                if status == "refused" and meta.get("retry_on_refusal") and idx == 0:
                    await self.fire("refused", task, attempt=attempt, model=label)
                    idx += 1
                    continue
                if limit and limit.scope == "model" and idx == 0:
                    # one model's usage cap, not the plan's ("switch models
                    # with /model"): retry once on the fallback, like a refusal
                    log.warning("task %s hit the %s usage limit; retrying on fallback",
                                task["id"], label)
                    await self.fire("refused", task, attempt=attempt, model=label,
                                    reason="usage_limit")
                    idx += 1
                    continue
                if status == "failed" and resume_id and is_resume_error(meta.get("error")):
                    # the saved session vanished under us: forget it and rerun
                    # the same model fresh — at most once, resume_id only goes
                    # one way (to None)
                    log.warning("task %s: resume of %s failed; retrying fresh",
                                task["id"], resume_id)
                    self.quick_sessions.pop(task["source"], None)
                    resume_id = None
                    continue

                final = "done" if status == "done" else "failed"
                if (final == "done" and meta.get("session_id")
                        and self.cfg.quick_session_idle_minutes):
                    self.quick_sessions[task["source"]] = (
                        meta["session_id"], time.monotonic())
                speech = (limits.limit_speech(limit)
                          if final == "failed" and limit else None)
                self.db.set_task_status(task["id"], final)
                self._capture_episode(task, final, "".join(collected) or None)
                finished = True
                await self.fire(final, task, cost_usd=meta.get("cost_usd"),
                                model=label, error=meta.get("error"), speech=speech)
                yield ("done", {
                    "task_id": task["id"], "status": final,
                    "cost_usd": meta.get("cost_usd"), "model": meta.get("model", label),
                    "error": meta.get("error"), "speech": speech,
                })
                return
        finally:
            if not finished:
                if open_run is not None:
                    self.db.finish_run(open_run, "cancelled",
                                       error="client disconnected mid-stream")
                self.db.set_task_status(task["id"], "cancelled")

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
        run_started = time.time()
        meta = json.loads(task["metadata"]) if task.get("metadata") else {}
        area = self.areas.get(task["area"]) if task.get("area") else None
        tools = (
            meta.get("allowed_tools")                                # agent-file override
            or (area.allowed_tools if area and area.allowed_tools else None)
            or self.cfg.tools_for(meta.get("task_type"))
        )
        system_extra = self._with_memory(area.context() if area else "")
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
                    resume_session_id=meta.get("resume_session_id"),
                    max_cost_usd=meta.get("max_cost_usd"),
                )
            # cancel() may have killed the subprocess and closed the books while
            # run_once was returning; don't overwrite 'cancelled' with 'failed'
            current = self.db.get_task(task["id"])
            if current and current["status"] == "cancelled":
                return
            output_path = runner.guess_output_path(task["text"], self.cfg.root)
            self.db.finish_run(
                run_id, result["status"],
                stop_reason=result.get("stop_reason"), cost_usd=result.get("cost_usd"),
                input_tokens=result.get("input_tokens"), output_tokens=result.get("output_tokens"),
                num_turns=result.get("num_turns"), session_id=result.get("session_id"),
                output_text=result.get("output_text"), error=result.get("error"),
                output_path=output_path,
            )
            limit = (limits.classify_limit(result.get("error"))
                     if result["status"] == "failed" else None)
            if result["status"] == "refused" and attempt == 1:
                log.warning("task %s refused on %s; retrying on fallback", task["id"], label)
                await self.fire("refused", task, attempt=attempt, model=label)
                continue
            if limit and limit.scope == "model" and attempt == 1:
                # one model's usage cap, not the plan's: retry once on the
                # fallback model, like a refusal
                log.warning("task %s hit the %s usage limit; retrying on fallback",
                            task["id"], label)
                await self.fire("refused", task, attempt=attempt, model=label,
                                reason="usage_limit")
                continue

            final = "done" if result["status"] == "done" else "failed"
            speech = (limits.limit_speech(limit)
                      if final == "failed" and limit else None)
            requeue_delay = None
            if (final == "failed" and limit and task["source"] == "timer"
                    and meta.get("limit_requeues", 0) < 2):
                requeue_delay = self._limit_requeue_delay(limit)
                speech += " I'll retry the task after that."
            self.db.set_task_status(task["id"], final)
            self._capture_episode(task, final, result.get("output_text"))
            await self.fire(final, task, cost_usd=result.get("cost_usd"),
                            model=label, output_path=output_path,
                            error=result.get("error"), speech=speech)
            if requeue_delay is not None:
                self._schedule_limit_requeue(task, meta, requeue_delay)
                await self.fire("requeued", task, delay_s=int(requeue_delay))
            if final == "done":
                if meta.get("task_type") in ("reflection", "learn"):
                    self._record_skill_patches(run_started)
                await self._maybe_reflect(task, meta, result)
            return

    async def _maybe_reflect(self, task: dict, task_meta: dict, result: dict):
        """Queue the post-task reflection fork (Phase G) when the run was
        complex enough to have taught something. The reflection resumes the
        run's own CLI session (warm cache) with areas/**-scoped edit tools."""
        lcfg = getattr(self.cfg, "learning", None) or {}
        if not reflection.should_reflect(lcfg, task, task_meta, result):
            return
        log.info("task %s: %s turns — queueing reflection",
                 task["id"], result.get("num_turns"))
        await self.submit(
            reflection.PROMPT, source="reflection", mode="agentic",
            match_area=False,  # the prompt's own text must not match triggers
            metadata={
                "task_type": "reflection",
                "resume_session_id": result["session_id"],
                "allowed_tools": list(reflection.REFLECTION_TOOLS),
                "max_cost_usd": lcfg.get("reflection_max_cost_usd", 1.00),
            })

    def _record_skill_patches(self, since: float):
        """Attribute area edits made by a reflection/learn run to skill
        telemetry: any area dir with a file modified after the run started
        counts as patched. mtime-based on purpose — the JSON output format
        carries no per-tool events (a run_steps table is Phase H)."""
        areas_dir = self.cfg.root / "areas"
        if not areas_dir.is_dir():
            return
        for d in areas_dir.iterdir():
            if not d.is_dir() or d.name.startswith("."):
                continue
            try:
                touched = any(f.is_file() and f.stat().st_mtime >= since - 1
                              for f in d.rglob("*"))
            except OSError:
                continue
            if touched:
                try:
                    self.db.record_skill_patch(d.name)
                except Exception:
                    log.exception("skill-patch bump failed for %s", d.name)

    def _limit_requeue_delay(self, limit: limits.LimitInfo) -> float:
        """Seconds until a limit-failed timer task is worth retrying: shortly
        after the advertised reset when one was parsed, else 30 minutes (the
        openclaw quota-suspension default)."""
        if limit.resets_at:
            secs = (limit.resets_at - datetime.now(timezone.utc)).total_seconds() + 120
            return min(max(secs, 60.0), 6 * 3600.0)
        return 1800.0

    def _schedule_limit_requeue(self, task: dict, meta: dict, delay: float):
        """Re-submit a timer task once the usage-limit window has reset, so a
        daily brief isn't lost to a morning limit. In-memory by design: a
        dispatcher restart drops the pending retry (startup reconciliation
        would fail a persisted queued row anyway); the journal and the
        'requeued' event record that it was scheduled."""
        key = f"requeue:{task['id']}"

        async def later():
            try:
                await asyncio.sleep(delay)
                await self.submit(
                    task["text"], source="timer", mode="agentic",
                    area=task.get("area"),
                    metadata={**meta, "limit_requeues": meta.get("limit_requeues", 0) + 1},
                )
            finally:
                self.bg.pop(key, None)

        self.bg[key] = asyncio.create_task(later())
        log.warning("task %s: usage limit hit; retry scheduled in %ds",
                    task["id"], int(delay))

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
