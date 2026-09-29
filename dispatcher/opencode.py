"""OpenCode backend: `opencode run --format json` as an alternative to the
Claude Code subscription CLI (locked decision #2 names `claude -p`; this
module adds a second backend behind the same service seams).

Selected with `backend: opencode` in config.yaml (default `claude`, so
existing checkouts keep running exactly as before). Both dispatch paths
branch on it: quick Q&A streams via :func:`stream`, agentic runs via
:func:`run_once` — same ``("delta", str)`` / ``("meta", dict)`` and
normalized-result shapes as :mod:`dispatcher.quick` / :mod:`dispatcher.runner`
so :mod:`dispatcher.service` only branches at the call site.

Free-tier constraint (measured 2026-09-29, NOT a hypothesis): the
``opencode/*-free`` / ``*-contributor-free`` models reject any run whose
project config carries ``tools`` or ``permission`` overrides — the API
answers ``FreeTierError: OpenCode's free tier can only be used from within
OpenCode`` (403), even for a prompt that uses no tools. Paid models accept
them but this account has no funds (402 ``Insufficient account funds``).
So per-task tool scoping is COARSE on this backend, by agent, not by tool:

- quick path and read-only agentic tasks → ``--agent plan`` (built-in,
  disallows all edit tools; verified read-only live: a write request
  returns a plan and leaves the file untouched).
- agentic tasks with any write-capable tool → ``--agent build`` (full
  tools — broader than the equivalent Claude ``Edit(vault/**)`` scoping).

That means the H1/H2 trust-boundary guarantee "granted tools mean exactly
these" does NOT hold tool-for-tool on the opencode path: a ``build`` run
can touch anything in the repo. The service-level boundary (untrusted
metadata can only narrow, never widen) still holds — it is enforced before
either backend is reached.

Other deliberate differences from the Claude path:

- ``opencode run`` has no ``--system-prompt`` / ``--append-system-prompt``
  flag, so the persona + memory-block context is prepended to the message
  itself (``SYSTEM … --- … USER``). The DB ``text`` keeps the raw prompt.
- No ``--max-budget-usd`` flag exists either: spend guard is the
  wall-clock timeout only (``budgets.timeout_s`` / ``quick_timeout_s``).
  ``models.effort`` is likewise not mapped (opencode ``--variant`` values
  are provider-specific; passing ``medium`` through would be a guess).
- No refusal signal: opencode emits no ``stop_reason: refusal``, so
  ``retry_on_refusal`` is always False and the fallback-model retry never
  triggers on this path. Usage-limit retries in the service still apply —
  they key off the error text, not the backend.
- Model IDs must be ``provider/model`` (e.g.
  ``opencode/muse-spark-1.3-contributor-free``). A configured value without
  a ``/`` (a Claude Code ID like ``claude-sonnet-5``) is ignored in favour
  of :data:`DEFAULT_MODEL` — passing it to ``-m`` would just fail.
- Session continuity is ``-s <sessionID>`` (verified live: a follow-up
  ``-s`` recalls the earlier turn), mirroring ``claude -p --resume``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from .config import Config

log = logging.getLogger("dispatcher.opencode")

BACKEND = "opencode"

DEFAULT_MODEL = "opencode/muse-spark-1.3-contributor-free"

# Built-in agents. `plan` is read-only (verified live); `build` is the
# default full-tools agent, passed explicitly so the choice is visible in
# process lists and logs rather than implied by omission.
QUICK_AGENT = "plan"
READONLY_AGENT = "plan"
WRITE_AGENT = "build"

# asyncio readline limit: one tool result arrives as one NDJSON line, same
# reasoning as runner.STREAM_LIMIT / quick.STREAM_LIMIT.
STREAM_LIMIT = 4 * 1024 * 1024
SUMMARY_LEN = 200

# Claude tool names (as the service spells them) that never write.
# Anything else — Edit/Write/MultiEdit/NotebookEdit/Bash(...), or an
# unknown name — takes the agentic run to the full-tools `build` agent.
# Conservative on purpose: an unrecognised tool must not silently land on
# the read-only agent and then fail confusingly, nor (worse) be treated as
# read-only when it writes.
_READ_ONLY_TOOLS = {"read", "glob", "grep"}


def is_read_only(tools: list[str] | None) -> bool:
    """True when this tool grant is safe on the read-only `plan` agent."""
    if not tools:
        return True
    return all((t or "").split("(")[0].strip().lower() in _READ_ONLY_TOOLS
               for t in tools)


def resolve_model(cfg: Config, key: str) -> str:
    """Model ID for the opencode path. A configured ``models.<key>`` wins
    only when it looks like an opencode ID (``provider/model``); a bare
    Claude Code ID falls back to :data:`DEFAULT_MODEL` (see module doc)."""
    v = (cfg.models or {}).get(key)
    if isinstance(v, str) and "/" in v:
        return v
    return DEFAULT_MODEL


def _message_with_system(text: str, system: str) -> str:
    if not system:
        return text
    return f"{system}\n\n---\n\n{text}"


def build_quick_cmd(text: str, cfg: Config, model: str | None,
                    system: str = "",
                    resume_session_id: str | None = None) -> list[str]:
    """``opencode run`` argv for one quick turn (read-only `plan` agent)."""
    cmd = [cfg.opencode_bin, "run", _message_with_system(text, system),
           "--format", "json",
           "--agent", QUICK_AGENT,
           "-m", model or resolve_model(cfg, "quick")]
    if resume_session_id:
        cmd += ["-s", resume_session_id]
    return cmd


def build_agentic_cmd(text: str, cfg: Config, model: str | None,
                      tools: list[str] | None, system: str = "",
                      resume_session_id: str | None = None) -> list[str]:
    """``opencode run`` argv for one headless attempt. Agent follows the
    tool grant coarsely (see module doc): read-only → `plan`, else `build`."""
    agent = READONLY_AGENT if is_read_only(tools) else WRITE_AGENT
    cmd = [cfg.opencode_bin, "run", _message_with_system(text, system),
           "--format", "json",
           "--agent", agent,
           "-m", model or resolve_model(cfg, "agentic")]
    if resume_session_id:
        cmd += ["-s", resume_session_id]
    return cmd


def _clip(s: str) -> str:
    s = " ".join(str(s or "").split())
    return s[:SUMMARY_LEN]


def _text_of(part: dict) -> str:
    return str((part or {}).get("text") or "")


def _tool_summary(part: dict) -> str:
    part = part or {}
    state = part.get("state") or {}
    return _clip(f"{part.get('tool')} {json.dumps(state.get('input') or {})}")


def _parse_line(obj: dict, steps: list[dict], t0: float,
                texts: list[str] | None = None) -> dict | None:
    """Fold one NDJSON event into steps/texts. Returns an ``error`` event's
    message, if this line is one, else None. Never raises on odd shapes —
    garbage in, skipped line out (same posture as the Claude parsers)."""
    try:
        kind = obj.get("type")
        part = obj.get("part") or {}
        ptype = part.get("type")
        now_ms = int((time.monotonic() - t0) * 1000)
        if kind == "step_start":
            steps.append({"type": "init", "summary": "session start",
                          "elapsed_ms": now_ms})
        elif kind == "text" and ptype == "text":
            body = _text_of(part)
            if body.strip():
                steps.append({"type": "text", "summary": _clip(body),
                              "elapsed_ms": now_ms})
                if texts is not None:
                    texts.append(body)
        elif kind == "tool_use":
            steps.append({"type": "tool_use",
                          "summary": _tool_summary(part),
                          "elapsed_ms": now_ms})
            out = (part.get("state") or {}).get("output")
            if out and texts is not None:
                texts.append(str(out))
        elif kind == "step_finish":
            sub = (part.get("reason") or "result")
            steps.append({"type": "result", "summary": _clip(sub),
                          "elapsed_ms": now_ms})
            return None
        elif kind == "error":
            err = obj.get("error") or {}
            msg = err.get("message") if isinstance(err, dict) else str(err)
            if not msg and isinstance(err, dict):
                msg = json.dumps(err)[:400]
            return str(msg or "opencode error")
    except Exception:
        log.exception("opencode event parse failed")
    return None


async def stream(text: str, cfg: Config, model_override: str | None = None,
                 tools: list[str] | None = None, context: str = "",
                 resume_session_id: str | None = None,
                 procs: dict | None = None, task_id: str | None = None):
    """Same shape as :func:`dispatcher.quick.stream`: yields ("delta", str)
    chunks then exactly one ("meta", dict). Quick always rides the
    read-only `plan` agent, so ``tools`` is accepted for signature parity
    and ignored. ``model_override`` is used verbatim (the service passes
    the cheap meta model for plumbing tasks); otherwise the configured
    opencode model or :data:`DEFAULT_MODEL`."""
    async for item in _stream_cli(text, cfg, model_override, context,
                                  resume_session_id, procs, task_id):
        yield item


async def _stream_cli(text: str, cfg: Config, model_override: str | None,
                      context: str = "",
                      resume_session_id: str | None = None,
                      procs: dict | None = None, task_id: str | None = None):
    from . import runner  # local import: runner never imports this module

    system = runner.AGENT_SYSTEM + ("\n\n" + context if context else "")
    # NOTE: quick persona parity — the Claude path answers as Jarvis the
    # voice assistant (QUICK_SYSTEM in quick.py). Prepend it here too so
    # the opencode quick turn gets the same "short spoken sentences" steer.
    from .quick import QUICK_SYSTEM
    system = QUICK_SYSTEM + "\n\n" + system
    model = model_override or resolve_model(cfg, "quick")
    cmd = build_quick_cmd(text, cfg, model_override, system,
                          resume_session_id)

    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cfg.root, env=dict(os.environ),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        limit=STREAM_LIMIT,
    )
    if procs is not None and task_id is not None:
        procs[task_id] = proc
    stderr_task = asyncio.create_task(proc.stderr.read())
    meta = {"backend": BACKEND, "model": model,
            "status": "failed", "retry_on_refusal": False}
    saw_delta = False
    session_id: str | None = None
    error: str | None = None
    cost: float | None = None
    in_tok: int | None = None
    out_tok: int | None = None
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
                if not isinstance(obj, dict):
                    continue
                session_id = obj.get("sessionID") or session_id
                if obj.get("type") == "error":
                    error = _parse_line(obj, [], time.monotonic()) or error
                    continue
                # accumulate cost/tokens off step_finish legs (free-tier
                # models report 0 cost; the sum stays correct regardless)
                if obj.get("type") == "step_finish":
                    toks = ((obj.get("part") or {}).get("tokens")) or {}
                    if isinstance(toks, dict):
                        if toks.get("input") is not None:
                            in_tok = (in_tok or 0) + (toks.get("input") or 0)
                        if toks.get("output") is not None:
                            out_tok = (out_tok or 0) + (toks.get("output") or 0)
                    part = obj.get("part")
                    c = (part.get("cost") if isinstance(part, dict) else None)
                    if c is None:
                        c = obj.get("cost")
                    if isinstance(c, (int, float)):
                        cost = (cost or 0) + c
                # text events stream; tool I/O stays in the run record, not
                # the spoken answer (matches the Claude path's delta discipline)
                if obj.get("type") == "text" and (obj.get("part") or {}).get("type") == "text":
                    body = _text_of(obj.get("part"))
                    if body.strip():
                        saw_delta = True
                        yield ("delta", body)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        meta.update({"status": "failed",
                     "error": f"opencode quick timeout after {timeout}s"})
    finally:
        if procs is not None and task_id is not None:
            procs.pop(task_id, None)
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        stderr_bytes = await runner.reap_reader(stderr_task)
    if error is not None:
        # an {"type":"error"} event arrived (auth, funds, denied tool …)
        meta.update({"status": "failed", "error": error})
    elif meta.get("error") is not None:
        pass  # timeout already recorded above
    elif proc.returncode not in (0, None) and not saw_delta:
        stderr = stderr_bytes[:500].decode(errors="replace")
        meta.update({"status": "failed",
                     "error": f"opencode exited {proc.returncode}: {stderr}"})
    else:
        meta.update({"status": "done", "error": None})
    meta.update({"session_id": session_id, "cost_usd": cost,
                 "input_tokens": in_tok, "output_tokens": out_tok,
                 "streamed": saw_delta})
    yield ("meta", meta)


async def run_once(text: str, cfg: Config, model: str | None, tools: list[str],
                   procs: dict, task_id: str, system_extra: str = "",
                   resume_session_id: str | None = None,
                   max_cost_usd: float | None = None,
                   on_step=None) -> dict:
    """One headless attempt via ``opencode run``. Same normalized result as
    :func:`dispatcher.runner.run_once` (``status``/``cost_usd``/``session_id``/
    ``output_text``/``steps`` …). ``max_cost_usd`` is accepted and ignored —
    opencode has no budget flag (see module doc); the wall-clock timeout
    bounds the run. Never raises (except CancelledError, like the Claude
    runner): a crash is a failed result, not an exception."""
    try:
        return await _attempt(text, cfg, model, tools, procs, task_id,
                              system_extra, resume_session_id, on_step)
    except Exception as e:
        log.exception("opencode run_once crashed for task %s", task_id)
        return {"status": "failed",
                "error": f"internal error: {type(e).__name__}: {e}"}


async def _attempt(text: str, cfg: Config, model: str | None,
                   tools: list[str], procs: dict, task_id: str,
                   system_extra: str = "",
                   resume_session_id: str | None = None,
                   on_step=None) -> dict:
    from . import runner  # AGENT_SYSTEM + reap_reader live there

    system = runner.AGENT_SYSTEM + ("\n\n" + system_extra if system_extra else "")
    cmd = build_agentic_cmd(text, cfg, model, tools, system,
                            resume_session_id)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cfg.root, env=dict(os.environ),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=STREAM_LIMIT,
        )
    except OSError as exc:
        return {"status": "failed", "error": f"spawn error: {exc}"}
    procs[task_id] = proc
    stderr_task = asyncio.create_task(proc.stderr.read())
    timeout = cfg.budgets.get("timeout_s", 600)
    t0 = time.monotonic()
    steps: list[dict] = []
    texts: list[str] = []
    session_id: str | None = None
    error: str | None = None
    cost: float | None = None
    in_tok: int | None = None
    out_tok: int | None = None
    turns = 0
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
                if not isinstance(obj, dict):
                    continue
                session_id = obj.get("sessionID") or session_id
                if obj.get("type") == "step_start":
                    turns += 1
                if obj.get("type") == "step_finish":
                    toks = ((obj.get("part") or {}).get("tokens")) or {}
                    if isinstance(toks, dict):
                        if toks.get("input") is not None:
                            in_tok = (in_tok or 0) + (toks.get("input") or 0)
                        if toks.get("output") is not None:
                            out_tok = (out_tok or 0) + (toks.get("output") or 0)
                    c = ((obj.get("part") or {}).get("cost")
                         if isinstance(obj.get("part"), dict) else None)
                    if c is None:
                        c = obj.get("cost")
                    if isinstance(c, (int, float)):
                        cost = (cost or 0) + c
                n_before = len(steps)
                msg = _parse_line(obj, steps, t0, texts)
                if msg and error is None:
                    error = msg
                # live SSE mirror, like the Claude runner's on_step hook
                if on_step and len(steps) > n_before:
                    try:
                        await on_step(steps[-1])
                    except Exception:
                        log.exception("on_step hook failed")
            await proc.wait()
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return {"status": "timeout", "error": f"killed after {timeout}s"}
    finally:
        procs.pop(task_id, None)
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        stderr_bytes = await runner.reap_reader(stderr_task)

    if error is not None:
        return {"status": "failed", "steps": steps, "error": error,
                "session_id": session_id, "cost_usd": cost,
                "input_tokens": in_tok, "output_tokens": out_tok,
                "num_turns": turns}
    if proc.returncode not in (0, None) and not steps:
        stderr = stderr_bytes.decode(errors="replace")
        return {"status": "failed", "steps": steps,
                "error": f"no result event (exit {proc.returncode}): {stderr[:400]}"}
    return {"status": "done", "steps": steps,
            "stop_reason": "stop",
            "cost_usd": cost, "input_tokens": in_tok,
            "output_tokens": out_tok, "num_turns": turns,
            "session_id": session_id,
            "output_text": "".join(texts) or None}
