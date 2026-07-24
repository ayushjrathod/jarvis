"""Service layer: the single dispatch path (locked decision #1). Every task —
voice, UI, queue, timer — flows through here; nothing else invokes Claude.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from . import (automations, graph, limits, memory, notify, quick, reflection,
               runner, spotify, telemetry)
from .areas import AreaRegistry
from .classifier import classify
from .config import Config
from .db import Database
from .events import EventBus
from .hooks import HookRegistry

log = logging.getLogger("dispatcher.service")

TERMINAL = {"done", "failed", "cancelled"}

# Unrelated machine-submitted quick tasks must not chain each other's CLI
# sessions the way a human source's follow-ups do (Phase G lesson: meta-work
# leaking into conversational machinery causes weird cross-contamination).
NO_CONTINUITY_SOURCES = {"automation", "automation-parse", "notify-gate",
                         "media-parse", "graph-extract", "graph-reconcile"}

# Ask-about-my-screen: extra system context for screenshot-question tasks.
SCREEN_CONTEXT = (
    "The user is asking about a screenshot they just captured. Answer from "
    "what the image actually shows, in concise plain prose."
)


def resolve_screenshot(cfg: Config, name: str) -> Path | None:
    """Traversal-guarded lookup: a bare <name>.png directly inside
    screenshots_dir, existing — anything else is None."""
    if not name or not name.endswith(".png"):
        return None
    base = Path(cfg.screenshots_dir).resolve()
    p = (base / name).resolve()
    if p.parent != base or not p.is_file():
        return None
    return p


def make_ack(text: str) -> str:
    words = text.split()
    short = " ".join(words[:8])
    return f"On it — {short}{'…' if len(words) > 8 else ''}"


# Metadata keys an UNtrusted caller must never be able to widen with: granting
# tools or injecting a resume session. max_cost_usd is clamped (not dropped) so
# an external caller can still narrow the budget. Internal server spawns pass
# trusted=True and keep all three (they legitimately set them).
_TRUSTED_ONLY_META_KEYS = ("allowed_tools", "resume_session_id")


def sanitize_untrusted_metadata(metadata: dict | None, cost_cap: float) -> dict | None:
    """Enforce the trust boundary (H1) on metadata supplied by an external
    source (api/queue/voice/ui/screen): drop tool/session grants and clamp the
    budget to the configured cap. The invariant is that an untrusted task can
    NARROW its tools/budget but never WIDEN them, and can never inject a resume
    session. Everything else (screenshot, task_type, notify, …) passes through.
    Returns a sanitized copy; the input is not mutated."""
    if not metadata:
        return metadata
    clean = dict(metadata)
    stripped = [k for k in _TRUSTED_ONLY_META_KEYS if clean.pop(k, None) is not None]
    budget = clean.get("max_cost_usd")
    if budget is None:
        pass
    elif isinstance(budget, bool) or not isinstance(budget, (int, float)):
        # a non-numeric budget can't be honored as a narrowing — drop it so the
        # configured default cap applies at build time
        clean.pop("max_cost_usd", None)
        stripped.append("max_cost_usd")
    elif budget > cost_cap:
        clean["max_cost_usd"] = cost_cap
        stripped.append(f"max_cost_usd({budget}->{cost_cap})")
    if stripped:
        log.warning("sanitized untrusted metadata: %s", ", ".join(stripped))
    return clean


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
        self.areas = AreaRegistry(cfg.root / "areas",
                                  privileged_areas=cfg.privileged_areas)
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

    async def create_task(self, text, source, kind, area=None, metadata=None,
                          trusted=False) -> dict:
        # Trust boundary (H1): only server-internal spawns (trusted=True) may set
        # allowed_tools/resume_session_id or a budget above the cap. Everything
        # from an external source is sanitized before it is stored, so both the
        # quick and agentic paths read already-safe metadata off the row.
        if not trusted:
            metadata = sanitize_untrusted_metadata(
                metadata, self.cfg.budgets.get("max_cost_per_task_usd", 0.50))
        task = self.db.create_task(text, source, kind, area, metadata)
        if area:  # skill telemetry (Phase G) — best-effort, never blocks dispatch
            try:
                self.db.record_skill_use(area)
            except Exception:
                log.exception("skill-use bump failed for %s", area)
        await self.fire("queued", task)
        return task

    async def submit(self, text, source="api", mode="auto", area=None,
                     metadata=None, match_area=True, trusted=False) -> dict:
        """Fire-and-forget entry point (queue watcher, timers). Quick tasks run
        in the background with output stored in the run row; HTTP clients that
        want streamed quick answers go through create_task + stream_quick.

        trusted=True is set ONLY by server-internal spawns (reflection, notify
        gate, consolidation, graph, learn, limit-requeue) that legitimately pass
        allowed_tools/max_cost_usd/resume_session_id; external callers leave it
        False so their metadata can only narrow, never widen (H1)."""
        kind, area_name = self.route(text, mode, area, match_area)
        task = await self.create_task(text, source, kind, area_name, metadata,
                                      trusted=trusted)
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

    def _is_cancelled(self, task_id: str) -> bool:
        row = self.db.get_task(task_id)
        return bool(row and row["status"] == "cancelled")

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
            try:
                task_meta = json.loads(task["metadata"]) if task.get("metadata") else {}
            except (TypeError, ValueError):
                task_meta = {}

            # ask-screen: a screenshot-question task gets the Read tool and
            # image context; a missing/pruned file degrades to a plain answer
            shot = None
            if task_meta.get("screenshot"):
                shot = resolve_screenshot(self.cfg, task_meta["screenshot"])
                if shot:
                    if not q_tools or "Read" not in q_tools:
                        q_tools = list(q_tools or []) + ["Read"]
                    q_context = "\n\n".join(x for x in (q_context, SCREEN_CONTEXT) if x)
                else:
                    log.warning("task %s: screenshot %r not found; answering without it",
                                task["id"], task_meta["screenshot"])

            models = [None, self.cfg.models.get("fallback", "claude-opus-4-8")]
            # metadata override first: the notify gate resumes the settled
            # run's own session rather than this source's conversation
            resume_id = (task_meta.get("resume_session_id")
                         or self._fresh_quick_session(task["source"]))
            attempt, idx = 0, 0
            while idx < len(models):
                # cancel() may have fired between attempts (it kills the live
                # subprocess and sets 'cancelled'); don't spawn a fresh turn over
                # a cancelled task (M2).
                if self._is_cancelled(task["id"]):
                    finished = True
                    return
                model_override = models[idx]
                attempt += 1
                label = model_override or self.cfg.models.get("quick") or "cli-default"
                run_id = self.db.create_run(task["id"], attempt, label)
                open_run = run_id
                meta, collected = {}, []
                t0, delta_times = time.monotonic(), []
                # first turn — or a fresh retry after a vanished session —
                # must tell the model to Read the image; resumed follow-ups
                # already have it in context. The DB `text` stays the raw
                # question either way.
                send_text = task["text"]
                if shot and resume_id is None:
                    send_text = (
                        f"Use the Read tool to view the screenshot at {shot}, "
                        f"then answer this question about it: {task['text']}")
                try:
                    async for kind, payload in quick.stream(
                        send_text, self.cfg, model_override,
                        tools=q_tools, context=q_context,
                        resume_session_id=resume_id,
                        procs=self.procs, task_id=task["id"],
                    ):
                        if kind == "delta":
                            delta_times.append(time.monotonic())
                            collected.append(payload)
                            yield ("delta", {"text": payload})
                        else:
                            meta = payload
                except Exception as e:
                    log.exception("quick run failed for %s", task["id"])
                    meta = {"status": "failed", "error": f"{type(e).__name__}: {e}"}

                status = meta.get("status", "failed")
                lat = telemetry.stream_stats(t0, delta_times, meta.get("output_tokens"))
                self.db.finish_run(
                    run_id, status,
                    stop_reason=meta.get("stop_reason"), cost_usd=meta.get("cost_usd"),
                    input_tokens=meta.get("input_tokens"), output_tokens=meta.get("output_tokens"),
                    session_id=meta.get("session_id"), error=meta.get("error"),
                    output_text="".join(collected) or None, **lat,
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

                # cancel() may have killed the subprocess mid-stream and already
                # written 'cancelled'; the kill surfaces here as a failed/short
                # meta, so re-check before settling and don't overwrite it (M2).
                if self._is_cancelled(task["id"]):
                    finished = True
                    return
                final = "done" if status == "done" else "failed"
                if (final == "done" and meta.get("session_id")
                        and self.cfg.quick_session_idle_minutes
                        and task["source"] not in NO_CONTINUITY_SOURCES):
                    self.quick_sessions[task["source"]] = (
                        meta["session_id"], time.monotonic())
                speech = (limits.limit_speech(limit)
                          if final == "failed" and limit else None)
                self.db.set_task_status(task["id"], final)
                answer = "".join(collected) or None
                self._capture_episode(task, final, answer)
                finished = True
                suppress = self._maybe_notify(task, task_meta, final, answer,
                                              meta.get("session_id"))
                await self.fire(final, task, cost_usd=meta.get("cost_usd"),
                                model=label, error=meta.get("error"), speech=speech,
                                ttft_ms=lat.get("ttft_ms"),
                                tokens_per_s=lat.get("tokens_per_s"),
                                **({"surface": False} if suppress else {}))
                yield ("done", {
                    "task_id": task["id"], "status": final,
                    "cost_usd": meta.get("cost_usd"), "model": meta.get("model", label),
                    "error": meta.get("error"), "speech": speech,
                    "ttft_ms": lat.get("ttft_ms"),
                    "tokens_per_s": lat.get("tokens_per_s"),
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
            meta.get("allowed_tools")                       # trusted spawn override
            # agent file resolved from disk: an untrusted caller may NAME an
            # agent but never hand us its grants (see AreaRegistry.agent_tools)
            or self.areas.agent_tools(task.get("area"), meta.get("agent"))
            or (area.allowed_tools if area and area.allowed_tools else None)
            or self.cfg.tools_for(meta.get("task_type"))
        )
        system_extra = self._with_memory(area.context() if area else "")
        attempts = [
            (1, self.cfg.models.get("agentic")),
            (2, self.cfg.models.get("fallback", "claude-opus-4-8")),
        ]
        simulate = bool(meta.get("simulate_refusal"))

        async def on_step(step: dict):
            await self.fire("step", task, step_type=step["type"],
                            summary=step.get("summary"),
                            elapsed_ms=step.get("elapsed_ms"))

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
                    on_step=on_step,
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
            try:
                self.db.add_run_steps(run_id, result.get("steps") or [])
            except Exception:
                log.exception("run_steps persist failed for run %s", run_id)
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
            if (final == "failed"
                    and meta.get("task_type") == "memory-consolidate"
                    and meta.get("episode_ids")):
                # the episodes were marked consolidated at hand-off; the run
                # failed, so return them to the pool for the next pass (M6)
                try:
                    n = self.db.mark_episodes_unconsolidated(meta["episode_ids"])
                    log.warning("consolidation %s failed; %d episode(s) requeued",
                                task["id"], n)
                except Exception:
                    log.exception("episode roll-back failed for %s", task["id"])
            self._capture_episode(task, final, result.get("output_text"))
            suppress = self._maybe_notify(task, meta, final,
                                          result.get("output_text"),
                                          result.get("session_id"))
            await self.fire(final, task, cost_usd=result.get("cost_usd"),
                            model=label, output_path=output_path,
                            error=result.get("error"), speech=speech,
                            **({"surface": False} if suppress else {}))
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
            trusted=True,      # server spawn: sets tools/budget/resume itself
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

    # -- notify gate + automations (Phase I) --------------------------------

    def _maybe_notify(self, task: dict, task_meta: dict, final: str,
                      answer: str | None, session_id: str | None) -> bool:
        """Decide what happens to this settled task's completion event.
        Returns True when the plain done/failed event should carry
        surface=False (notice clients stay quiet) — either because the task
        is internal meta-work or because the notify gate takes over."""
        try:
            verdict = notify.surfacing(self.cfg.automations or {},
                                       task, task_meta, final)
        except Exception:
            log.exception("surfacing policy failed for %s", task["id"])
            return False
        if verdict == "gate":
            key = f"notify:{task['id']}"
            self.bg[key] = asyncio.create_task(
                self._notify_gate(task, answer, session_id, key))
            return True
        return verdict == "silent"

    async def _notify_gate(self, task: dict, answer: str | None,
                           session_id: str | None, key: str):
        """Run the notify-or-not judgment as its own quick task (cost stays
        in the books), resuming the settled run's session when it has one.
        Fail-open: any breakage notifies with a generic summary rather than
        silently swallowing a result the user asked for."""
        fallback = f"Finished: {task['text'][:100]}"
        try:
            meta = {"task_type": "notify-gate"}
            if session_id:
                meta["resume_session_id"] = session_id
            gate_task = await self.create_task(
                notify.gate_prompt(task["text"], answer),
                "notify-gate", "quick", None, meta, trusted=True)
            reply, status = await self._collect_quick(gate_task)
            verdict, text = (notify.parse_gate(reply) if status == "done"
                             else ("notify", ""))
            if verdict == "skip":
                log.info("notify gate: suppressed %s (%s)", task["id"], text)
                await self.fire("notify_skipped", task, reason=text)
                return
            await self._deliver_notice(task, text or fallback)
        except Exception:
            log.exception("notify gate crashed for %s; notifying anyway", task["id"])
            try:
                await self._deliver_notice(task, fallback)
            except Exception:
                log.exception("notify delivery failed for %s", task["id"])
        finally:
            self.bg.pop(key, None)

    async def _deliver_notice(self, task: dict, summary: str):
        log.info("notify: surfacing %s: %s", task["id"], summary[:120])
        await self.fire("notify", task, speech=summary, summary=summary)
        if (self.cfg.automations or {}).get("notify_desktop", True):
            await notify.send_desktop(summary)

    async def _collect_quick(self, task: dict) -> tuple[str, str]:
        """Drain a quick task synchronously, returning (answer, status) —
        for machine consumers of quick output (automation parse, notify gate)."""
        chunks, status = [], "failed"
        async for event, payload in self.stream_quick(task):
            if event == "delta":
                chunks.append(payload["text"])
            elif event == "done":
                status = payload.get("status", "failed")
        return "".join(chunks), status

    async def create_automation_from_nl(self, request: str, source: str
                                        ) -> tuple[dict | None, str]:
        """One LLM parse → mechanical validation → automations row.
        Returns (row_or_None, speakable_confirmation)."""
        task = await self.create_task(
            automations.parse_prompt(request), "automation-parse", "quick",
            None, {"task_type": "automation-parse"}, trusted=True)
        reply, status = await self._collect_quick(task)
        try:
            if status != "done":
                raise ValueError(f"parse task status {status}")
            spec = automations.validate_spec(automations.parse_response(reply))
            next_at = automations.next_run_iso(spec)
        except ValueError as e:
            log.warning("automation parse failed for %r: %s", request[:80], e)
            return None, "Sorry, I couldn't set that up as an automation."
        row = self.db.create_automation(request, source, spec, next_at)
        await self.hooks.fire({
            "event": "automation_created", "automation_id": row["id"],
            "text": spec["task_text"][:200], "next_run_at": next_at,
        })
        log.info("automation %s created: %s (%s)", row["id"],
                 spec["task_text"][:80], automations.describe(spec))
        return row, f"Scheduled: {spec['task_text']} — {automations.describe(spec)}."

    # -- media control ------------------------------------------------------

    async def media_command(self, text: str, source: str = "api") -> str:
        """Run a music command, returning the one sentence to speak.

        Deterministic first (no LLM, no tokens, sub-second). Only music-shaped
        text the parser can't resolve — "put on something chill" — costs a
        single `media-parse` quick call, which chooses SEARCH WORDS ONLY; the
        action itself is always executed by the deterministic path. That keeps
        the model out of the privileged loop: no area, no Bash, no D-Bus reach.
        """
        mcfg = self.cfg.media or {}
        intent = spotify.detect(text)
        if intent is None:
            return "I couldn't work out what to play."
        if intent is spotify.MAYBE:
            if not mcfg.get("llm_fallback", True):
                return "I couldn't work out what to play."
            intent = await self._parse_media(text, source)
            if intent is None:
                return "I couldn't work out what to play."
        return await asyncio.to_thread(spotify.run_intent, self.cfg, intent)

    async def _parse_media(self, text: str, source: str) -> "spotify.Intent | None":
        """One quick JSON call → mechanically validated play intent (the
        automation-parse pattern). None on anything malformed."""
        task = await self.create_task(
            spotify.parse_prompt(text), "media-parse", "quick", None,
            {"task_type": "media-parse", "origin_source": source}, trusted=True)
        reply, status = await self._collect_quick(task)
        try:
            if status != "done":
                raise ValueError(f"parse task status {status}")
            return spotify.validate_parsed(spotify.parse_response(reply))
        except ValueError as e:
            log.warning("media parse failed for %r: %s", text[:80], e)
            return None

    # -- knowledge graph (Phase J2/J3) --------------------------------------

    def graph_enabled(self) -> bool:
        return bool((self.cfg.memory.get("graph") or {}).get("enabled"))

    def _today(self) -> str:
        return datetime.now(timezone.utc).date().isoformat()

    async def graph_extract(self, episodes_text: str,
                            episode_ids: list[int]) -> dict | None:
        """Nightly fact extraction over the consolidation export: one quick
        JSON call, deterministic apply (graph.py). None = parse failure —
        the export file is still on disk for a manual replay."""
        candidates = graph.candidate_ids(self.db)  # the exact set the prompt shows
        task = await self.create_task(
            graph.extraction_prompt(self.db, episodes_text, self._today()),
            "graph-extract", "quick", None, {"task_type": "graph-extract"},
            trusted=True)
        reply, status = await self._collect_quick(task)
        try:
            if status != "done":
                raise ValueError(f"extract task status {status}")
            counts = graph.apply_extraction(
                self.db, graph.parse_reply(reply), episode_ids,
                allowed_ids=candidates)
        except ValueError as e:
            log.warning("graph extraction failed: %s", e)
            return None
        await self.hooks.fire({"event": "graph", **counts})
        log.info("graph extraction: %s", counts)
        return counts

    def spawn_graph_extract(self, episodes_text: str, episode_ids: list[int]):
        """Fire-and-forget alongside the consolidation agent (both read the
        same export; neither blocks the other)."""
        if not self.graph_enabled():
            return
        key = f"graph:{time.monotonic()}"

        async def run():
            try:
                await self.graph_extract(episodes_text, episode_ids)
            except Exception:
                log.exception("graph extract crashed")
            finally:
                self.bg.pop(key, None)

        self.bg[key] = asyncio.create_task(run())

    async def graph_reconcile(self) -> dict:
        """Weekly mem0-style pass: duplicates/contradictions/stale facts get
        invalidated (never deleted). Cheap no-op while the graph is small."""
        if len(self.db.active_facts(limit=2)) < 2:
            return {"status": "nothing_to_reconcile", **self.db.graph_counts()}
        candidates = graph.candidate_ids(self.db, graph.RECONCILE_LIMIT)
        task = await self.create_task(
            graph.reconcile_prompt(self.db, self._today()),
            "graph-reconcile", "quick", None, {"task_type": "graph-reconcile"},
            trusted=True)
        reply, status = await self._collect_quick(task)
        try:
            if status != "done":
                raise ValueError(f"reconcile task status {status}")
            counts = graph.apply_reconciliation(
                self.db, graph.parse_reply(reply), allowed_ids=candidates)
        except ValueError as e:
            log.warning("graph reconciliation failed: %s", e)
            return {"status": "failed", "error": str(e)}
        log.info("graph reconciliation: %s", counts)
        return {"status": "done", **counts, **self.db.graph_counts()}

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
                    # re-submitting an already-validated timer task: keep its
                    # (already-sanitized) metadata intact rather than re-clamping
                    trusted=True,
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
