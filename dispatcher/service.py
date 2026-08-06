"""Service layer: the single dispatch path (locked decision #1). Every task —
voice, UI, queue, timer — flows through here; nothing else invokes Claude.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import (automations, desktop, embeddings, graph, limits, memory, notify,
               offline, quick, reflection, runner, spotify, telemetry)
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
                         "media-parse", "graph-extract", "graph-reconcile",
                         # "inbox": two files landing in one poll are unrelated
                         # machine tasks, but the second used to resume the
                         # first's CLI session — so the model still had document
                         # A in context while summarizing document B, and could
                         # describe the wrong file (added 2026-08-08).
                         "inbox"}

# Internal plumbing tasks that ask for a *classification*, not prose: a
# NOTIFY/SKIP verdict, a schedule spec, a set of search words. Each has
# mechanical validation downstream (parse_gate / validate_spec / the
# deterministic media executor), so a small model is safe here — and the
# saving is not marginal. Measured on this box, one gate call:
#   sonnet + --resume  $0.141   (what these cost before 2026-07-26)
#   sonnet, fresh      $0.103
#   haiku,  fresh      $0.028
# The floor is the Claude Code system prompt itself (~16k cache-creation
# tokens on every cold `claude -p`), which is why the model rate dominates.
# graph-extract is deliberately NOT here: its output is model-authored fact
# text that lands in the knowledge graph, so quality outranks the ~$0.14.
# Override with `models.meta_task_types` in config.yaml.
DEFAULT_META_TASK_TYPES = ("notify-gate", "automation-parse", "media-parse")

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


def meta_model(cfg: Config, task_meta: dict) -> str | None:
    """The cheap model to run this quick task on, or None for the normal
    `models.quick`. None whenever the task isn't internal plumbing or
    `models.meta` is unset, so clearing that key restores the old behavior."""
    tt = (task_meta or {}).get("task_type")
    if not tt:
        return None
    types = cfg.models.get("meta_task_types", DEFAULT_META_TASK_TYPES)
    if tt not in types:
        return None
    return cfg.models.get("meta") or None


def make_ack(text: str) -> str:
    words = text.split()
    short = " ".join(words[:8])
    return f"On it — {short}{'…' if len(words) > 8 else ''}"


# Metadata keys an UNtrusted caller must never be able to widen with: granting
# tools, injecting a resume session, or naming episodes. max_cost_usd is
# clamped (not dropped) so an external caller can still narrow the budget.
# Internal server spawns pass trusted=True and keep all four (they
# legitimately set them). `episode_ids` joined this list 2026-08-06: the key
# names the batch a FAILED consolidation returns to the pool, and anyone
# reaching POST /task could previously hand any failing task a set of ids to
# resurrect — persisting across restarts via the startup sweep.
_TRUSTED_ONLY_META_KEYS = ("allowed_tools", "resume_session_id", "episode_ids")

# `simulate_refusal` forces the refusal fallback, i.e. a SECOND run of the same
# task on models.fallback (an Opus). It is a test seam, but it sat outside the
# trust boundary until 2026-08-08: anything that could reach POST /task — the
# dashboard, the phone over Tailscale Serve, a queue file — could double the
# cost of every agentic run and put the second half on the most expensive model.
# On a subscription whose plan cap has already taken the assistant down twice
# that is a real quota amplifier, so it is now stripped like the others.
# `scripts/smoke_phase_a.sh` genuinely needs it over HTTP, hence the opt-in:
# set `security.allow_simulate_refusal: true`, run the smoke, set it back.
_SMOKE_ONLY_META_KEYS = ("simulate_refusal",)


def sanitize_untrusted_metadata(metadata: dict | None, cost_cap: float,
                                allow_simulate_refusal: bool = False) -> dict | None:
    """Enforce the trust boundary (H1) on metadata supplied by an external
    source (api/queue/voice/ui/screen): drop tool/session grants and clamp the
    budget to the configured cap. The invariant is that an untrusted task can
    NARROW its tools/budget but never WIDEN them, and can never inject a resume
    session. Everything else (screenshot, task_type, notify, …) passes through.
    Returns a sanitized copy; the input is not mutated."""
    if not metadata:
        return metadata
    clean = dict(metadata)
    drop = _TRUSTED_ONLY_META_KEYS
    if not allow_simulate_refusal:
        drop += _SMOKE_ONLY_META_KEYS
    stripped = [k for k in drop if clean.pop(k, None) is not None]
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
        # ...and give back any episodes those orphans were holding. Without this
        # a consolidation interrupted by a restart stranded its whole batch
        # forever — marked consolidated at hand-off, never distilled, never
        # re-exported (added 2026-08-10).
        try:
            stranded = self.db.orphaned_consolidation_episode_ids()
            if stranded:
                n = self.db.mark_episodes_unconsolidated(stranded)
                if n:
                    log.warning("returned %d episode(s) from an interrupted "
                                "consolidation to the pool", n)
        except Exception:
            log.exception("startup episode roll-back failed")
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
        # desktop verbs awaiting a yes/no: confirm_id -> {intent, source,
        # expires}. In-memory like quick_sessions — a restart cancels pending
        # confirmations, which is the safe direction to fail.
        self.pending_desktop: dict[str, dict] = {}

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
                metadata, self.cfg.budgets.get("max_cost_per_task_usd", 0.50),
                self.cfg.allow_simulate_refusal)
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
        # Deterministic diverts run here, not only in the HTTP handler, so a
        # fired automation or a queue file gets the same treatment a typed
        # command does. Before this, "every morning play jazz" correctly became
        # a standing automation and then handed "play jazz" to Claude as a task
        # every morning, because the diverts lived in main.py alone.
        if mode == "auto":
            divert = await self.try_divert(text, source)
            if divert is not None:
                return await self._settle_divert(text, source, divert, metadata)
        kind, area_name = self.route(text, mode, area, match_area)
        task = await self.create_task(text, source, kind, area_name, metadata,
                                      trusted=trusted)
        if kind == "agentic":
            self.start_agentic(task)
        else:
            self.bg[task["id"]] = asyncio.create_task(self._drain_quick(task))
        return task

    # -- deterministic diverts ----------------------------------------------

    async def try_divert(self, text: str, source: str) -> dict | None:
        """The single definition of "this never needs a model": standing
        automation, then media, then desktop. Returns a dict carrying `kind`,
        `speech` and any per-kind extras, or None to route normally.

        Order matters and is load-bearing: automation first so "every morning
        play jazz" is scheduled rather than played once; media before desktop so
        "play …" stays with Spotify rather than being read as an app launch.
        Callers must gate on mode == "auto" — an explicit quick/agentic bypasses
        every divert, which is what keeps all of this reachable.
        """
        try:
            return await self._try_divert_inner(text, source)
        except Exception:
            # A divert is an optimization, never a requirement. Letting it raise
            # here 500s POST /task and files a queue file under .failed as a
            # TERMINAL error — for text that would have routed perfectly well.
            log.exception("divert failed for %r; routing normally", text[:80])
            return None

    async def _try_divert_inner(self, text: str, source: str) -> dict | None:
        acfg = self.cfg.automations or {}
        # An automation must never create another automation: a task_text the
        # parser left schedule words in would otherwise breed a fresh row on
        # every fire.
        if (acfg.get("enabled") and acfg.get("nl_detect", True)
                and source != "automation" and automations.detect(text)):
            row, speech = await self.create_automation_from_nl(text, source)
            if row is None:
                # The parse failed — which during a plan-cap window is the
                # NORMAL outcome, since the parse is itself a quick task.
                # Returning a divert here consumed the user's request: it was
                # neither scheduled nor run, and (over submit()) was filed
                # 'done'. Fall through and let it route like any other text.
                log.info("automation parse failed; routing %r normally", text[:80])
                return None
            return {"kind": "automation", "speech": speech, "ok": True,
                    "automation_id": row["id"]}

        mcfg = self.cfg.media or {}
        if (mcfg.get("enabled") and mcfg.get("nl_detect", True)
                and spotify.detect(text) is not None):
            outcome = await self.media_command(text, source)
            return {"kind": "media", "speech": outcome["speech"],
                    "ok": outcome["status"] == "done"}

        ccfg = self.cfg.computer or {}
        if (ccfg.get("enabled") and ccfg.get("nl_detect", True)
                and (desktop.detect(text) is not None
                     or (self.pending_desktop_for(source)
                         and desktop.parse_answer(text) is not None))):
            outcome = await self.desktop_command(text, source)
            return {"kind": "desktop", "speech": outcome["speech"],
                    "ok": outcome["status"] == "done",
                    "desktop_status": outcome["status"],
                    "confirm_id": outcome.get("confirm_id")}
        return None

    async def _settle_divert(self, text: str, source: str, divert: dict,
                             metadata: dict | None = None) -> dict:
        """Record a diverted command as a task row that is born settled.

        submit()'s callers (the queue watcher, automations.fire) need a task
        dict back, and an audit trail for "the 07:00 automation played jazz" is
        worth having anyway — over HTTP these commands leave no trace at all.
        No run row: no model ran, so there is no attempt, cost or latency to
        log, and /stats must not count one. No episode either — see
        memory.should_capture for why a nightly "play jazz" must not become
        knowledge.

        Two things this got wrong until 2026-08-08, both of them the failure
        mode this codebase keeps re-learning:

        - It filed **every** divert 'done', including the ones that did not
          act: a desktop verb parked for confirmation nobody can answer (the
          queue and automation sources have no user watching), a denied verb,
          an unresolved play. `ok` now comes from the executor.
        - It **announced nothing**. It fires the plain `done` event, but the
          voice client drops everything with `kind != "agentic"`
          (jarvis/engines/brain_dispatcher.py) and nothing on this path called
          send_desktop — so a fired "play jazz" was silent, where before the
          diverts existed it had gone through the notify gate and been spoken.
          Delivery now goes through the same `notify` event the inbox watcher
          uses, which clients actually consume.
        """
        final = "done" if divert.get("ok") else "failed"
        # Caller metadata (automations.fire passes automation_id/notify) is
        # kept: dropping it silently broke the task->automation link on the row.
        # But it crosses UNsanitized no longer: queue-file frontmatter used to
        # reach create_task marked trusted, the one place an untrusted dict
        # crossed the boundary as trusted. trusted=False still keeps
        # automation_id/notify (not in the drop list) while stripping anything
        # a caller must never set; task_type/divert are assigned after the
        # merge below, so a forged task_type cannot survive either.
        meta = {**(metadata or {}),
                "task_type": "divert", "divert": divert["kind"]}
        task = await self.create_task(text, source, "quick", None, meta,
                                      trusted=False)
        self.db.set_task_status(task["id"], final)
        await self.fire(final, task, speech=divert["speech"],
                        divert=divert["kind"], cost_usd=0.0)
        await self._announce_divert(task, meta, final, divert["speech"])
        return {**task, "status": final, "divert": divert}

    async def _announce_divert(self, task: dict, meta: dict, final: str,
                               speech: str):
        """Speak/notify a machine-submitted divert's own sentence.

        Consults the ordinary surfacing policy (so `notify: false` on an
        automation still means quiet), but never the gate: the executor already
        wrote the one-line summary, and paying a model to rewrite "Playing Kind
        of Blue by Miles Davis." would be pure waste — that is what
        notify.NEVER_GATE_TASK_TYPES encodes, and routing through here is what
        makes it reachable at all rather than dead code.
        """
        try:
            verdict = notify.surfacing(self.cfg.automations or {},
                                       task, meta, final)
            if verdict == "silent":
                return
            await self._deliver_notice(task, speech)
        except Exception:
            log.exception("divert announcement failed for %s", task["id"])

    # -- quick path ---------------------------------------------------------

    def _fresh_quick_session(self, source: str) -> str | None:
        """Session to resume for a follow-up turn, or None for a fresh start.
        Idle-freshness per openclaw reset-policy.ts: the source's last session
        holds until quick_session_idle_minutes pass without a completed turn
        (0 = continuity off)."""
        idle_min = self.cfg.quick_session_idle_minutes
        entry = self.quick_sessions.get(source)
        if not idle_min:
            return None
        # Sweep every expired source, not just the one being looked up (fixed
        # 2026-08-10). Entries were only ever dropped when that SAME source was
        # queried again and found stale — but ask-about-my-screen uses a fresh
        # source per screenshot (`screen:<shot_id>`), so each one is looked up a
        # couple of times and then never again, leaving a permanent dict entry
        # for the life of the process. Small, but monotonic and unbounded.
        cutoff = time.monotonic() - idle_min * 60
        for src in [s for s, (_, used) in self.quick_sessions.items() if used <= cutoff]:
            self.quick_sessions.pop(src, None)
        entry = self.quick_sessions.get(source)
        return entry[0] if entry else None

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
            q_context = self._with_memory(area.context() if area else "")
            try:
                task_meta = json.loads(task["metadata"]) if task.get("metadata") else {}
            except (TypeError, ValueError):
                task_meta = {}
            # A trusted internal spawn's own grant wins, then the area's. The
            # metadata half was missing until 2026-08-08, so a quick task that
            # asked for tools silently got NONE — inbox.summarize ships
            # ["Read", "Glob", "Grep"] with a prompt that says "Read it", and
            # was shipping `--disallowedTools Bash` and no grant at all: it
            # spent a real call per file on an answer the model could not
            # ground. Safe to read here because sanitize_untrusted_metadata
            # strips allowed_tools BEFORE it is stored (H1), so anything still
            # on the row came from a server-internal spawn — exactly the
            # reasoning the agentic resolver below already relies on.
            q_tools = (task_meta.get("allowed_tools")
                       or (area.quick_allowed_tools if area else None))

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

            # first entry: the cheap model for internal classification tasks,
            # None (= models.quick) for everything else. Refusal/limit retries
            # still climb to the fallback model.
            models = [meta_model(self.cfg, task_meta),
                      self.cfg.models.get("fallback", "claude-opus-4-8")]
            # metadata override first: an internal spawn may name the session it
            # wants continued rather than this source's conversation
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
                # A cancel that already landed wins (fixed 2026-08-10). An HTTP
                # quick task is never registered in self.bg, so cancel() only
                # kills the subprocess — after it has written 'cancelled' — and
                # the kill surfaces here as EOF with no result event, i.e.
                # status "failed" and error "claude exited -9: …". That then
                # overwrote the run row's honest 'cancelled', and it happens on
                # EVERY voice barge-in, which is exactly this path. The task row
                # was already correct (the _is_cancelled guard further down);
                # only the run row and its error string were wrong.
                if status != "done" and self._is_cancelled(task["id"]):
                    status = "cancelled"
                    meta = {**meta, "error": None}
                lat = telemetry.stream_stats(t0, delta_times, meta.get("output_tokens"),
                                             streamed=meta.get("streamed", True))
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
                # Plan-wide cap: don't just report the outage — answer from the
                # local index if it holds anything (no model, no generation).
                degraded = None
                if final == "failed" and limit and not collected:
                    degraded = await self._degraded_answer(task, speech)
                    if degraded:
                        speech = degraded
                        yield ("delta", {"text": degraded})
                self.db.set_task_status(task["id"], final)
                answer = "".join(collected) or degraded
                # Capture the MODEL's words only — never the degraded answer.
                # A degraded answer is text this system composed out of its own
                # index during an outage; storing it as an episode indexes the
                # outage message, and the next question during the same outage
                # can then match it and quote "Claude's session limit is hit
                # right now…" back under "here's what I already have on it".
                # That is the self-observation loop 5f200f3 closed for the daily
                # brief, re-opened on a new path (found 2026-08-08).
                self._capture_episode(task, final, "".join(collected) or None)
                finished = True
                suppress = self._maybe_notify(task, task_meta, final, answer,
                                              meta.get("session_id"))
                await self.fire(final, task, cost_usd=meta.get("cost_usd"),
                                model=label, error=meta.get("error"), speech=speech,
                                ttft_ms=lat.get("ttft_ms"),
                                tokens_per_s=lat.get("tokens_per_s"),
                                **({"degraded": True} if degraded else {}),
                                **({"surface": False} if suppress else {}))
                yield ("done", {
                    "task_id": task["id"], "status": final,
                    "cost_usd": meta.get("cost_usd"), "model": meta.get("model", label),
                    "error": meta.get("error"), "speech": speech,
                    "ttft_ms": lat.get("ttft_ms"),
                    "tokens_per_s": lat.get("tokens_per_s"),
                    # tells the client the text it just got came from the local
                    # index, not from Claude
                    **({"degraded": True} if degraded else {}),
                })
                return
        except (GeneratorExit, asyncio.CancelledError):
            # the client really did vanish mid-stream (barge-in closed the
            # connection, browser tab died) — settled as cancelled below
            raise
        except Exception:
            # anything else is OUR failure, not the client's. It used to land in
            # the same `finally` and be filed as 'cancelled', which reads as a
            # user action and hides a dispatcher bug from /stats entirely.
            log.exception("stream_quick crashed for %s", task["id"])
            finished = True
            try:
                # task status first: it is the row everything else reads, and
                # the DB call that just failed may well be the one we're about
                # to make again
                self.db.set_task_status(task["id"], "failed")
                if open_run is not None:
                    self.db.finish_run(open_run, "failed",
                                       error="dispatcher error")
            except Exception:
                # the database itself may be what broke — startup orphan
                # reconciliation is the backstop for whatever we can't close
                log.exception("could not settle %s after a crash", task["id"])
            raise
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

    def _rollback_episodes(self, task: dict, meta: dict | None = None):
        """Return a consolidation run's episodes to the unconsolidated pool.

        Episodes are marked consolidated at HAND-OFF (main.py), so any way the
        run fails to actually distil them strands that batch forever: nothing
        re-exports them, and the only artefact left is the export file, which
        nobody is prompted to replay. Until 2026-08-10 this ran on exactly one
        path — `_run_agentic_inner` settling 'failed' — leaving three holes:
        cancellation, the outer crash handler, and a dispatcher restart
        mid-run (which `reconcile_orphans` settles as failed with no idea the
        task owned episodes). Safe to call more than once: the UPDATE only
        touches rows that are still marked, and a no-op logs nothing.
        """
        try:
            if meta is None:
                row = self.db.get_task(task["id"]) or {}
                meta = json.loads(row["metadata"]) if row.get("metadata") else {}
        except (TypeError, ValueError):
            meta = {}
        if (meta or {}).get("task_type") != "memory-consolidate":
            return
        # Only the timer's own consolidation may return episodes. task_type and
        # episode_ids are caller-settable on the wire (episode_ids is stripped
        # for untrusted callers now, but rows predate the fix and defense
        # belongs at the use site too): without the source gate, any failing
        # task claiming to be a consolidation resurrected arbitrary episodes.
        source = task.get("source")
        if source is None:
            try:
                row = self.db.get_task(task["id"]) or {}
                source = row.get("source")
            except Exception:
                source = None
        if source != "timer":
            return
        ids = (meta or {}).get("episode_ids")
        if not ids:
            return
        try:
            n = self.db.mark_episodes_unconsolidated(ids)
            if n:
                log.warning("consolidation %s did not complete; %d episode(s) "
                            "returned to the pool", task["id"], n)
        except Exception:
            log.exception("episode roll-back failed for %s", task["id"])

    async def _run_agentic(self, task: dict):
        try:
            async with self.sem:
                await self._run_agentic_inner(task)
        except asyncio.CancelledError:
            # cancel() already wrote state and fired the event — but it cannot
            # know this task was holding a batch of episodes hostage.
            self._rollback_episodes(task)
        except Exception:
            log.exception("agentic task %s crashed", task["id"])
            self.db.set_task_status(task["id"], "failed")
            self._rollback_episodes(task)
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
            if final == "failed":
                self._rollback_episodes(task, meta)
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
        in the books). Fail-open: any breakage notifies with a generic summary
        rather than silently swallowing a result the user asked for.

        Deliberately does NOT resume the settled run's session (it did until
        2026-07-26). The gate prompt already inlines the request and result,
        so resuming only replays a long agentic transcript as input for a
        one-line verdict — measured $0.141 resumed vs $0.103 fresh on the same
        model. `session_id` is kept in the signature for callers/tests.
        """
        fallback = f"Finished: {task['text'][:100]}"
        try:
            meta = {"task_type": "notify-gate"}
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

    async def media_command(self, text: str, source: str = "api") -> dict:
        """Run a music command → {speech, status}, mirroring desktop_command.

        Deterministic first (no LLM, no tokens, sub-second). Only music-shaped
        text the parser can't resolve — "put on something chill" — costs a
        single `media-parse` quick call, which chooses SEARCH WORDS ONLY; the
        action itself is always executed by the deterministic path. That keeps
        the model out of the privileged loop: no area, no Bash, no D-Bus reach.

        Returns a status rather than a bare sentence because `_settle_divert`
        has to know whether anything actually happened: a machine-submitted
        divert used to be filed 'done' even when the answer was "I couldn't
        work out what to play." (fixed 2026-08-08).
        """
        mcfg = self.cfg.media or {}
        unresolved = {"status": "unresolved",
                      "speech": "I couldn't work out what to play."}
        intent = spotify.detect(text)
        if intent is None:
            return unresolved
        if intent is spotify.MAYBE:
            if not mcfg.get("llm_fallback", True):
                return unresolved
            intent = await self._parse_media(text, source)
            if intent is None:
                return unresolved
        speech = await asyncio.to_thread(spotify.run_intent, self.cfg, intent)
        # NOTE: spotify.run_intent's contract is "never raises — every failure
        # becomes a speakable sentence", so a dead player still returns
        # status=done with "Spotify isn't running." in the text. Distinguishing
        # that needs run_intent itself to report a status; left as-is.
        return {"status": "done", "speech": speech}

    # -- desktop control (computer-use T1) ----------------------------------

    async def desktop_command(self, text: str, source: str) -> dict:
        """Deterministic desktop verb → {speech, status, confirm_id?}.

        Mirrors media_command, with the safety plane in front: `policy()`
        decides allow / confirm / deny per verb. A "confirm" verdict parks the
        intent in `self.pending_desktop` and returns needs_confirmation — the
        caller surfaces it (SSE `confirm` event + spoken question) and the user
        answers through `confirm_desktop`. No LLM anywhere on this path.
        """
        # A bare "yeah"/"no" answers this source's parked confirmation — that's
        # what makes the confirm plane usable by voice, where there are no
        # buttons. Only consulted when that source really has one pending, so a
        # stray "yes" in conversation can't trigger a desktop verb.
        cid = self.pending_desktop_for(source)
        if cid:
            answer = desktop.parse_answer(text)
            if answer is not None:
                return await self.confirm_desktop(cid, answer)

        intent = desktop.detect(text)
        if intent is None:
            return {"status": "unrecognized",
                    "speech": "I couldn't work out what to do on the desktop."}
        return await self.run_desktop_intent(intent, source)

    async def _degraded_answer(self, task: dict, limit_speech: str | None) -> str | None:
        """Answer a rate-limited question from the local index instead of
        dying until the window resets. Off by default for internal plumbing
        sources — a machine task wants a real failure, not a consolation
        paragraph it might act on."""
        if not (self.cfg.memory or {}).get("offline_fallback", True):
            return None
        if task["source"] in NO_CONTINUITY_SOURCES:
            return None
        try:
            hits = await asyncio.to_thread(
                offline.search_memory, self.cfg, self.db, embeddings, task["text"])
        except Exception:
            log.exception("offline fallback failed for %s", task["id"])
            return None
        log.info("task %s rate-limited; answering from memory (%d hit(s))",
                 task["id"], len(hits))
        return offline.compose(limit_speech or "Claude is unavailable right now.",
                               hits)

    def pending_desktop_for(self, source: str) -> str | None:
        """The newest unexpired confirmation parked for this source, if any."""
        self._expire_desktop_confirms()
        hits = [(e["expires"], c) for c, e in self.pending_desktop.items()
                if e["source"] == source]
        return max(hits)[1] if hits else None

    async def run_desktop_intent(self, intent, source: str) -> dict:
        ccfg = self.cfg.computer or {}
        verdict = desktop.policy(ccfg, intent.verb)
        if verdict == "deny":
            log.info("desktop: denied %s (policy)", intent.verb)
            return {"status": "denied",
                    "speech": f"I'm not allowed to {intent.describe()}."}
        if verdict == "confirm":
            cid = uuid.uuid4().hex[:12]
            timeout_s = ccfg.get("confirm_timeout_s", 120)
            self.pending_desktop[cid] = {
                "intent": intent, "source": source,
                "expires": time.monotonic() + timeout_s,
            }
            self._expire_desktop_confirms()
            speech = f"Shall I {intent.describe()}?"
            # no task row backs a desktop verb, so build the event payload
            # directly rather than going through fire(event, task, …)
            await self.hooks.fire({
                "event": "confirm", "task_id": None, "kind": "desktop",
                "source": source, "text": intent.describe(),
                "confirm_id": cid, "verb": intent.verb,
                # The client can't know the window otherwise, so a dashboard
                # banner sat there offering Yes on an id the server had already
                # expired — clicking it reported "expired" with no warning.
                "timeout_s": timeout_s,
                "description": intent.describe(), "speech": speech,
            })
            log.info("desktop: %s awaiting confirmation (%s)", intent.verb, cid)
            return {"status": "needs_confirmation", "confirm_id": cid,
                    "speech": speech}
        speech = await asyncio.to_thread(desktop.run_intent, intent)
        log.info("desktop: ran %s -> %s", intent.verb, speech[:80])
        return {"status": "done", "speech": speech}

    async def confirm_desktop(self, confirm_id: str, approve: bool) -> dict:
        """Answer a parked desktop confirmation. Unknown/expired ids are
        reported rather than silently executed."""
        self._expire_desktop_confirms()
        entry = self.pending_desktop.pop(confirm_id, None)
        if entry is None:
            return {"status": "expired",
                    "speech": "That request already expired."}
        if not approve:
            log.info("desktop: user declined %s", entry["intent"].verb)
            return {"status": "declined", "speech": "Okay, skipping it."}
        speech = await asyncio.to_thread(desktop.run_intent, entry["intent"])
        log.info("desktop: confirmed %s -> %s", entry["intent"].verb, speech[:80])
        return {"status": "done", "speech": speech}

    def _expire_desktop_confirms(self):
        now = time.monotonic()
        for cid in [c for c, e in self.pending_desktop.items()
                    if e["expires"] <= now]:
            self.pending_desktop.pop(cid, None)

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
        # Marked only here, on the success path: a parse failure or a
        # rate-limited run leaves the episodes unmarked so the next pass
        # retries them, which is the same fail-toward-retry direction
        # `mark_episodes_unconsolidated` takes for the consolidation half.
        self.db.mark_episodes_graph_extracted(episode_ids)
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
