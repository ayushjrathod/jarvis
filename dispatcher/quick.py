"""Quick-path streaming (locked decision #2: quick Q&A streams immediately).

Two backends behind one async-generator interface that yields ("delta", str)
chunks followed by exactly one ("meta", dict):

- messages_api: Anthropic Messages API stream. On Fable 5 the server-side
  fallback beta retries refusals on the fallback model inside the same call,
  so meta["retry_on_refusal"] is False.
- claude_cli: `claude -p --output-format stream-json` — works with the
  subscription OAuth login when no API credentials exist on the machine.
  Refusal retry is the dispatcher's job here (meta["retry_on_refusal"] True).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid

from . import runner
from .config import Config

log = logging.getLogger("dispatcher.quick")

# stdout line budget for stream-json: asyncio's 64KiB readline default is too
# small once quick tools are allowed (one Read result arrives as one JSON line)
STREAM_LIMIT = 4 * 1024 * 1024

QUICK_SYSTEM = (
    "You are Jarvis, a concise personal voice assistant. Answer directly in "
    "one to three short sentences suitable for being read aloud. Plain prose "
    "only: no markdown, no lists, no preamble."
)

FALLBACK_BETA = "server-side-fallback-2026-06-01"


# One Anthropic client per event loop, kept warm. Building a fresh
# AsyncAnthropic per turn means a fresh connection pool and a fresh TLS
# handshake on every voice question — measured at ~300ms of the TTFT on this
# box (1826ms first call vs 1526ms steady state with the client reused). The
# loop is part of the key because the underlying httpx client is bound to one;
# the dispatcher has a single loop, while tests spin up a new one per asyncio.run.
_client_cache: tuple | None = None


def api_client():
    """The warm AsyncAnthropic for the running loop, created on first use
    (never at import — there may be no credentials)."""
    global _client_cache
    from anthropic import AsyncAnthropic

    loop = asyncio.get_running_loop()
    if _client_cache is not None and _client_cache[0] is loop:
        return _client_cache[1]
    client = AsyncAnthropic()
    _client_cache = (loop, client)
    return client


async def close_api_client():
    """Release the pooled connections on dispatcher shutdown."""
    global _client_cache
    if _client_cache is None:
        return
    client = _client_cache[1]
    _client_cache = None
    try:
        await client.close()
    except Exception:
        log.debug("closing the anthropic client failed", exc_info=True)


class HistoryStore:
    """Conversation history for the Messages API backend.

    The CLI backend gets continuity for free: `claude -p --resume` keeps the
    transcript on disk under a session id the CLI mints. The Messages API is
    stateless — every request must carry the whole conversation — so this is
    the equivalent store, and it is deliberately keyed the same way. It mints a
    session id and returns it in `meta`, so the continuity machinery already in
    service.py (`quick_sessions`, `metadata.resume_session_id`) works unchanged
    for both backends and nothing above this line has to care which is live.

    Bounded on both axes, because unlike the CLI's on-disk store this lives in
    the dispatcher's memory and every turn is re-sent as input tokens: at most
    `max_turns` exchanges per session (oldest dropped first) and `max_sessions`
    sessions (least-recently-used evicted).
    """

    def __init__(self, max_turns: int = 6, max_sessions: int = 32):
        self.max_turns = max(1, max_turns)
        self.max_sessions = max(1, max_sessions)
        self._sessions: dict[str, list[dict]] = {}

    def start(self, resume_session_id: str | None) -> tuple[str, list[dict]]:
        """(session_id, prior messages). An unknown id starts a fresh session
        rather than failing — the CLI path's vanished-session retry has no
        equivalent here because there is nothing to be stale about."""
        if resume_session_id and resume_session_id in self._sessions:
            prior = self._sessions.pop(resume_session_id)      # pop+reinsert
            self._sessions[resume_session_id] = prior          # = LRU touch
            return resume_session_id, list(prior)
        return uuid.uuid4().hex, []

    def record(self, session_id: str, user_text: str, assistant_text: str):
        msgs = self._sessions.pop(session_id, [])
        msgs = msgs + [{"role": "user", "content": user_text},
                       {"role": "assistant", "content": assistant_text}]
        del msgs[:max(0, len(msgs) - self.max_turns * 2)]
        self._sessions[session_id] = msgs
        while len(self._sessions) > self.max_sessions:
            self._sessions.pop(next(iter(self._sessions)))     # oldest touch
        return msgs

    def forget(self, session_id: str):
        self._sessions.pop(session_id, None)

    def __len__(self):
        return len(self._sessions)


class ApiAbort:
    """Cancel handle for an in-flight Messages API turn.

    Duck-types the two subprocess attributes `Service.cancel`/`shutdown` touch
    (`returncode`, `kill()`) so a barge-in aborts an API turn through exactly
    the same path that kills a CLI subprocess — locked decision #7 applies to
    both backends, and neither of those callers needs to know the difference.
    """

    def __init__(self):
        self.returncode = None

    def kill(self):
        self.returncode = -1

    @property
    def aborted(self) -> bool:
        return self.returncode is not None


# A key that authenticates but can't be used — no credit, revoked, wrong
# permissions. This matters because `quick_backend: auto` routes to the API the
# instant the env var exists: without a fallback, exporting an unfunded key
# would take the whole quick path down (every voice question 400s), which is
# exactly what a credit-exhausted key did here on 2026-07-27.
API_UNUSABLE_HINTS = (
    "credit balance is too low", "invalid x-api-key", "authentication_error",
    "permission_error", "billing", "quota",
)
API_COOLDOWN_S = 900.0
_api_disabled_until = 0.0


def api_unusable(exc: BaseException) -> bool:
    """True when the error means "this key can't serve requests", as opposed to
    a transient or request-specific failure worth surfacing."""
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(h in text for h in API_UNUSABLE_HINTS)


def disable_api(reason: str = ""):
    """Stop auto-routing to the API for a cooldown, so a dud key costs one
    failed round-trip every 15 minutes instead of one per question."""
    global _api_disabled_until
    _api_disabled_until = asyncio.get_event_loop().time() + API_COOLDOWN_S
    log.warning("messages_api disabled for %.0fs: %s", API_COOLDOWN_S, reason)


def _api_on_cooldown() -> bool:
    try:
        return asyncio.get_event_loop().time() < _api_disabled_until
    except RuntimeError:
        return False


def resolve_backend(cfg: Config) -> str:
    if cfg.quick_backend in ("messages_api", "claude_cli"):
        return cfg.quick_backend
    # env vars only: they are what the SDK actually reads — a credentials file
    # on disk proves nothing and would route to a backend that can't auth
    has_creds = bool(
        os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    )
    if has_creds and _api_on_cooldown():
        return "claude_cli"
    return "messages_api" if has_creds else "claude_cli"


async def stream(text: str, cfg: Config, model_override: str | None = None,
                 tools: list[str] | None = None, context: str = "",
                 resume_session_id: str | None = None,
                 procs: dict | None = None, task_id: str | None = None,
                 history: HistoryStore | None = None):
    """procs/task_id let the caller register the in-flight turn for cancel/
    barge-in (mirrors runner.run_once): while a turn is streaming,
    procs[task_id] holds the live subprocess (CLI) or an ApiAbort handle
    (Messages API) so POST /task/{id}/cancel can stop it.

    `history` is the Messages API conversation store; the CLI backend ignores
    it because `--resume` keeps its own transcript."""
    backend = resolve_backend(cfg)
    if tools and backend == "messages_api":
        backend = "claude_cli"  # file-reading quick queries need CLI tool access
    if backend == "messages_api":
        # An unusable key must never take the assistant down: fall through to
        # the CLI backend, which needs no credit (subscription OAuth). Only
        # safe while nothing has been emitted — once deltas are on the wire the
        # answer is half-spoken and restarting would repeat it.
        emitted = False
        try:
            async for item in _stream_api(text, cfg, model_override, context,
                                          resume_session_id, history, procs, task_id):
                emitted = emitted or item[0] == "delta"
                yield item
            return
        except Exception as e:
            if emitted or not api_unusable(e):
                raise
            disable_api(f"{type(e).__name__}: {e}"[:200])
    agen = _stream_cli(text, cfg, model_override, tools, context,
                       resume_session_id, procs, task_id)
    async for item in agen:
        yield item


async def _stream_api(text: str, cfg: Config, model_override: str | None,
                      context: str = "", resume_session_id: str | None = None,
                      history: HistoryStore | None = None,
                      procs: dict | None = None, task_id: str | None = None):
    model = model_override or cfg.models.get("quick", "claude-fable-5")
    # the memory blocks ride the system prompt, exactly as on the CLI path —
    # without this, switching backends would silently drop locked decision #4
    system = QUICK_SYSTEM + ("\n\n" + context if context else "")
    history = history if history is not None else HistoryStore()
    session_id, prior = history.start(resume_session_id)

    abort = ApiAbort()
    if procs is not None and task_id is not None:
        procs[task_id] = abort      # let cancel()/barge-in stop this turn (M2)

    kwargs = dict(
        model=model,
        max_tokens=cfg.budgets.get("quick_max_tokens", 1024),
        system=system,
        messages=prior + [{"role": "user", "content": text}],
    )
    collected: list[str] = []
    client = api_client()      # warm + pooled; NOT closed here (see api_client)
    if model.startswith("claude-fable"):
        ctx = client.beta.messages.stream(
            **kwargs,
            betas=[FALLBACK_BETA],
            fallbacks=[{"model": cfg.models.get("fallback", "claude-opus-4-8")}],
        )
        retry_on_refusal = False  # server already tried the fallback chain
    else:
        ctx = client.messages.stream(**kwargs)
        retry_on_refusal = True
    async with ctx as s:
        async for delta in s.text_stream:
            # granularity is one delta, the same bar the CLI path meets by
            # reading a line at a time; leaving the `async with` closes the
            # HTTP stream, so the turn really does stop
            if abort.aborted:
                log.info("quick api turn %s aborted mid-stream", task_id)
                yield ("meta", {"backend": "messages_api", "model": model,
                                "status": "cancelled", "session_id": session_id,
                                "retry_on_refusal": False})
                return
            collected.append(delta)
            yield ("delta", delta)
        final = await s.get_final_message()
    usage = final.usage
    refused = final.stop_reason == "refusal"
    answer = "".join(collected)
    # a refusal is not a conversational turn worth replaying on the next
    # question, and recording it would poison every follow-up in the session
    if answer and not refused:
        history.record(session_id, text, answer)
    yield ("meta", {
        "backend": "messages_api",
        "model": final.model,
        "status": "refused" if refused else "done",
        "stop_reason": final.stop_reason,
        "session_id": session_id,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cost_usd": cfg.quick_cost(final.model, usage.input_tokens, usage.output_tokens),
        "retry_on_refusal": retry_on_refusal and refused,
    })


async def _stream_cli(text: str, cfg: Config, model_override: str | None,
                      tools: list[str] | None = None, context: str = "",
                      resume_session_id: str | None = None,
                      procs: dict | None = None, task_id: str | None = None):
    system = QUICK_SYSTEM + ("\n\n" + context if context else "")
    cmd = [
        cfg.claude_bin, "-p", text,
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--max-budget-usd", str(cfg.budgets.get("quick_max_cost_usd", 0.10)),
        "--system-prompt", system,
    ]
    if resume_session_id:
        cmd += ["--resume", resume_session_id]
    if tools:
        cmd += ["--allowedTools", ",".join(tools)]
    if not runner.grants_bash(tools):
        # mirror the agentic runner: the CLI's sandboxed read-only Bash
        # auto-permit is disabled unless Bash was explicitly granted
        cmd += ["--disallowedTools", "Bash"]
    model = model_override or cfg.models.get("quick")
    if model:
        cmd += ["--model", model]
    effort = cfg.models.get("effort")
    if effort:
        cmd += ["--effort", effort]
    # subscription auth, never the API key — see runner.cli_env
    env = runner.cli_env(cfg)

    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cfg.root, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        limit=STREAM_LIMIT,
    )
    if procs is not None and task_id is not None:
        procs[task_id] = proc  # let cancel()/barge-in kill this live turn (M2)
    # drain stderr concurrently: with only stdout being read, a chatty CLI can
    # fill the 64KiB stderr pipe and deadlock until the timeout kills it
    stderr_task = asyncio.create_task(proc.stderr.read())
    meta = {"backend": "claude_cli", "model": model or "cli-default",
            "status": "failed", "retry_on_refusal": False}
    saw_delta = False
    timeout = cfg.budgets.get("quick_timeout_s", 120)
    try:
        async with asyncio.timeout(timeout):
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "stream_event":
                    ev = obj.get("event", {})
                    delta = ev.get("delta", {})
                    if ev.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                        saw_delta = True
                        yield ("delta", delta.get("text", ""))
                elif obj.get("type") == "result":
                    refused = _cli_refused(obj)
                    error = None
                    if obj.get("is_error"):
                        # a failed run may carry its message in "result" or only
                        # in the "errors" array (e.g. a dead --resume session)
                        error = obj.get("result") or "; ".join(
                            str(e) for e in obj.get("errors") or []) or None
                    meta.update({
                        "status": "refused" if refused
                        else ("failed" if obj.get("is_error") else "done"),
                        "stop_reason": obj.get("stop_reason") or obj.get("subtype"),
                        "cost_usd": obj.get("total_cost_usd"),
                        "input_tokens": (obj.get("usage") or {}).get("input_tokens"),
                        "output_tokens": (obj.get("usage") or {}).get("output_tokens"),
                        "session_id": obj.get("session_id"),
                        "retry_on_refusal": refused,
                        "error": error,
                    })
                    if not saw_delta and not obj.get("is_error") and obj.get("result"):
                        yield ("delta", obj["result"])
            await proc.wait()
    except TimeoutError:
        proc.kill()
        await proc.wait()
        meta.update({"status": "failed", "error": f"quick timeout after {timeout}s"})
    finally:
        if procs is not None and task_id is not None:
            procs.pop(task_id, None)
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        # always reap the concurrent stderr drainer (L2)
        stderr_bytes = await runner.reap_reader(stderr_task)
    if proc.returncode not in (0, None) and meta["status"] == "failed" and not meta.get("error"):
        stderr = stderr_bytes[:500].decode(errors="replace")
        meta["error"] = f"claude exited {proc.returncode}: {stderr}"
    yield ("meta", meta)


def _cli_refused(result: dict) -> bool:
    if result.get("stop_reason") == "refusal":
        return True
    return "refusal" in str(result.get("subtype", ""))
