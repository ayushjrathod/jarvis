"""Agentic headless runner: `claude -p` with guardrails, refusal fallback
(locked decision #3), and full run logging.

Guardrails per unattended run: --allowedTools (per task type, read-only
baseline), --max-budget-usd, and a wall-clock timeout. This CLI version has no
--max-turns flag; the budget cap bounds runaway loops instead. Never uses
permission-skipping flags.

Phase H: output format is stream-json so every message becomes a timeline
step (run_steps table + live SSE) instead of being discarded — the run's
result object carries the same fields the old json format did.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

from .config import Config

log = logging.getLogger("dispatcher.runner")

# one tool result arrives as one JSON line; asyncio's 64KiB default is too small
STREAM_LIMIT = 4 * 1024 * 1024
SUMMARY_LEN = 200

AGENT_SYSTEM = (
    "You are an unattended background agent of the mission-control system. "
    "The working directory is the mission-control repository; keep any file "
    "outputs inside it. Complete the task fully, then stop. No one can answer "
    "questions mid-run."
)

OUTPUT_PATH_RE = re.compile(r"\b(?:vault|data|queue|areas)/[\w./-]+\.\w{1,5}\b")


def is_refusal(result: dict) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("stop_reason") == "refusal":
        return True
    if "refusal" in str(result.get("subtype", "")):
        return True
    msg = result.get("message")
    return isinstance(msg, dict) and msg.get("stop_reason") == "refusal"


def guess_output_path(task_text: str, root) -> str | None:
    m = OUTPUT_PATH_RE.search(task_text)
    if m and (root / m.group(0)).exists():
        return m.group(0)
    return None


def wrote_declared_output(task_text: str, root, since: float) -> bool | None:
    """Did a prompt that names an output file ("write it to vault/briefs/x.md")
    actually produce it during this run? None when the prompt names no file.

    ANY named file, not the first: the consolidation prompt names its INPUT
    (data/consolidation/<stamp>.md, written before submit so its mtime always
    predates the run) in step 1 and its outputs later — first-match made this
    permanently False for that task type, so one harmless denial force-failed
    a good run, rolled the episodes back, and re-ran the same batch every
    night. mtime, not mere existence: re-running a brief on a day whose file
    already exists must not mask a denial that stopped it being refreshed."""
    paths = [m.group(0) for m in OUTPUT_PATH_RE.finditer(task_text)]
    if not paths:
        return None
    for rel in paths:
        try:
            if (Path(root) / rel).stat().st_mtime >= since:
                return True
        except OSError:
            continue
    return False


# Whitelist for the one-time transient retry below: a spawn error (CLI binary
# momentarily missing/unreadable) or a wall-clock timeout. Never retried here:
# refusals and budget-exceeded, which are deliberate terminal outcomes handled
# by the caller's model-fallback logic instead.
SPAWN_ERRORS = (OSError,)


async def run_once(text: str, cfg: Config, model: str | None, tools: list[str],
                   procs: dict, task_id: str, system_extra: str = "",
                   resume_session_id: str | None = None,
                   max_cost_usd: float | None = None,
                   on_step=None) -> dict:
    """One `claude -p` attempt (with one internal retry on a transient
    spawn/timeout error). Returns normalized result fields incl. `steps`.
    resume_session_id continues an earlier CLI session (reflection forks ride
    the warm prompt cache); max_cost_usd overrides the default budget cap;
    on_step is an async callback fired per timeline step as it happens. Never
    raises (except CancelledError, which is BaseException and always
    propagates): an unexpected crash inside _attempt used to skip finish_run
    entirely, stranding the run row `running` forever while the task read
    failed with no diagnosis. A crash is a failed result with the traceback
    in the error, not an exception."""
    try:
        return await _run_with_retry(
            text, cfg, model, tools, procs, task_id,
            system_extra, resume_session_id, max_cost_usd, on_step)
    except Exception as e:
        log.exception("run_once crashed for task %s", task_id)
        return {"status": "failed",
                "error": f"internal error: {type(e).__name__}: {e}"}


async def _run_with_retry(text: str, cfg: Config, model: str | None,
                          tools: list[str],
                          procs: dict, task_id: str, system_extra: str = "",
                          resume_session_id: str | None = None,
                          max_cost_usd: float | None = None,
                          on_step=None) -> dict:
    """One `claude -p` attempt (with one internal retry on a transient
    spawn/timeout error). Returns normalized result fields incl. `steps`.
    resume_session_id continues an earlier CLI session (reflection forks ride
    the warm prompt cache); max_cost_usd overrides the default budget cap;
    on_step is an async callback fired per timeline step as it happens."""
    for attempt in (1, 2):
        try:
            return await _attempt(text, cfg, model, tools, procs, task_id,
                                  system_extra, resume_session_id, max_cost_usd,
                                  on_step)
        except _Timeout as exc:
            # A timeout is NOT retried (changed 2026-08-10). It used to be, and
            # that was wrong twice over: a run that hit the 600s wall-clock was
            # usually *working*, so re-running the same prompt from scratch with
            # the same write grants meant a second pass over files the first
            # attempt had already edited. And the first attempt raises before any
            # `result` event, so it reports no cost_usd, no session_id and no
            # steps — up to 600s of real model work invisible to /stats, with
            # both attempts sharing the single runs row. Only a spawn failure is
            # genuinely transient; a timeout is a verdict.
            return {"status": "timeout", "error": str(exc)}
        except SPAWN_ERRORS as exc:
            if attempt == 2:
                return {"status": "failed", "error": f"spawn error: {exc}"}
        delay = cfg.budgets.get("transient_retry_delay_s", 2)
        log.warning("transient error on task %s attempt %d, retrying in %ss", task_id, attempt, delay)
        await asyncio.sleep(delay)


class _Timeout(Exception):
    def __init__(self, seconds):
        super().__init__(f"killed after {seconds}s")
        self.seconds = seconds


# The CLI backend is the SUBSCRIPTION path — that is the whole reason it
# exists here (CLAUDE.md: no API credentials, subscription OAuth only). But the
# `claude` CLI prefers an API key over the claude.ai login whenever one is in
# the environment, and says so:
#   "claude.ai connectors are disabled because ANTHROPIC_API_KEY or another
#    auth source is set and takes precedence over your claude.ai login"
# So an exported key doesn't just add an option, it *replaces* the subscription
# — and with an unfunded key every `claude -p` fails outright (observed
# 2026-07-27). Even with a funded one it would silently bill CLI runs, at ~18k
# system-prompt tokens each, to API credit instead of the subscription.
# Strip it. There is no API path left to fall back to — the Messages API
# backend was removed 2026-07-27 (recoverable at ce93ea0); the subscription CLI
# is the only backend, which is exactly why a stray key must never shadow it.
_STRIPPED_CLI_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def cli_env(cfg) -> dict:
    """Environment for a `claude` subprocess: inherited, pinned to the
    project's CLAUDE_CONFIG_DIR, minus any API credentials."""
    env = {k: v for k, v in os.environ.items() if k not in _STRIPPED_CLI_VARS}
    if cfg.claude_config_dir:
        env["CLAUDE_CONFIG_DIR"] = cfg.claude_config_dir
    return env


def grants_bash(tools: list[str] | None) -> bool:
    return any(t == "Bash" or t.startswith("Bash(") for t in tools or [])


async def reap_reader(task, timeout: float = 5) -> bytes:
    """Await a `StreamReader.read()` task to completion (bounded), cancelling it
    if it somehow hangs, so the concurrent stderr drainer is never left orphaned
    when the main read loop exits (L2). Returns whatever bytes it read, or b''."""
    try:
        return await asyncio.wait_for(task, timeout)
    except Exception:
        if not task.done():
            task.cancel()
        return b""


def build_cmd(text: str, cfg: Config, model: str | None, tools: list[str],
              system_extra: str = "", resume_session_id: str | None = None,
              max_cost_usd: float | None = None) -> list[str]:
    system = AGENT_SYSTEM + ("\n\n" + system_extra if system_extra else "")
    # `is None`, not `or` (fixed 2026-08-10): sanitize_untrusted_metadata lets a
    # caller NARROW the budget and passes 0 through unchanged, and `0 or 0.50`
    # is 0.50 — so asking for the tightest possible cap silently produced the
    # loosest configured one. Not an escalation (only a narrowing was lost), but
    # the exact opposite of what was asked for.
    budget = (cfg.budgets.get("max_cost_per_task_usd", 0.50)
              if max_cost_usd is None else max_cost_usd)
    cmd = [
        cfg.claude_bin, "-p", text,
        "--output-format", "stream-json",
        "--verbose",
        "--max-budget-usd", str(budget),
        "--append-system-prompt", system,
    ]
    if resume_session_id:
        cmd += ["--resume", resume_session_id]
    if tools:
        cmd += ["--allowedTools", ",".join(tools)]
    if not grants_bash(tools):
        # the headless CLI auto-permits sandboxed read-only Bash (cwd-scoped,
        # no writes) even when Bash is absent from --allowedTools — probed
        # 2026-07-19. Granting exactly these tools should mean exactly these.
        cmd += ["--disallowedTools", "Bash"]
    if model:
        cmd += ["--model", model]
    effort = cfg.models.get("effort")
    if effort:
        cmd += ["--effort", effort]
    return cmd


def _clip(s: str) -> str:
    s = " ".join(str(s).split())
    return s[:SUMMARY_LEN]


# The headless CLI reports a missing --allowedTools grant as an ordinary
# is_error tool_result and then lets the model carry on, so the run still ends
# subtype=success. That's how three daily briefs "succeeded" while writing
# nothing (2026-07-21..23). A denial here is always an operator-side grant
# mismatch, never something the model can fix, so we surface it as a failure.
_DENIAL_MARKERS = ("requested permissions", "haven't granted it yet",
                   "permission to use", "permission denied")


def is_permission_denial(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _DENIAL_MARKERS)


# ...but only a denial of a tool that could have written the file explains a
# missing output. Gmail MCP cannot: the daily brief is *designed* to omit its
# email section silently when that grant is absent, and it has never been
# granted (the agent asks for `mcp__gmail`, the server is
# `mcp__claude_ai_Gmail__*`), so that denial fires on every single run. On
# 2026-07-23 and 2026-07-30 it turned a run that legitimately made no edit
# — yesterday's catch-up had already written the file — into a hard `failed`.
# An unparseable denial stays fatal: silently reporting success while writing
# nothing is the failure mode session 17 exists to prevent.
_WRITE_CAPABLE_TOOLS = {"write", "edit", "multiedit", "notebookedit", "bash"}
_DENIED_TOOL_RE = re.compile(r"to use ([A-Za-z_][\w.-]*)")


def denial_could_block_output(summary: str) -> bool:
    """Could this denied tool have been what stopped the output file appearing?"""
    m = _DENIED_TOOL_RE.search(summary or "")
    if not m:
        return True
    return m.group(1).split("(")[0].strip().lower() in _WRITE_CAPABLE_TOOLS


def _tool_result_text(content) -> str:
    if isinstance(content, list):
        content = " ".join(b.get("text", "") for b in content
                           if isinstance(b, dict) and b.get("type") == "text")
    return str(content or "")


def _steps_from_event(obj: dict) -> list[dict]:
    """Timeline steps for one stream-json line. Summaries are clipped — the
    timeline is for orientation, full output stays in the run row."""
    t = obj.get("type")
    if t == "system" and obj.get("subtype") == "init":
        return [{"type": "init", "summary": _clip(obj.get("model") or "session start")}]
    if t == "assistant":
        steps = []
        for block in (obj.get("message") or {}).get("content") or []:
            if block.get("type") == "text" and block.get("text", "").strip():
                steps.append({"type": "text", "summary": _clip(block["text"])})
            elif block.get("type") == "tool_use":
                steps.append({"type": "tool_use", "summary": _clip(
                    f"{block.get('name')} {json.dumps(block.get('input') or {})}")})
        return steps
    if t == "user":
        steps = []
        for block in (obj.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                body = _tool_result_text(block.get("content"))
                prefix = "ERROR: " if block.get("is_error") else ""
                step = {"type": "tool_result", "summary": _clip(prefix + body)}
                # flagged on the full text: a long path can push the giveaway
                # phrase past the summary clip
                if block.get("is_error") and is_permission_denial(body):
                    step["denied"] = True
                steps.append(step)
        return steps
    if t == "result":
        return [{"type": "result", "summary": _clip(obj.get("subtype") or "result")}]
    return []


async def _attempt(text: str, cfg: Config, model: str | None, tools: list[str],
                    procs: dict, task_id: str, system_extra: str = "",
                    resume_session_id: str | None = None,
                    max_cost_usd: float | None = None,
                    on_step=None) -> dict:
    cmd = build_cmd(text, cfg, model, tools, system_extra,
                    resume_session_id, max_cost_usd)
    env = cli_env(cfg)

    # capture before spawn: any output the run writes must post-date this, or a
    # stale prior file could vouch for a run that actually wrote nothing
    wall_t0 = time.time()
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cfg.root, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        limit=STREAM_LIMIT,
    )
    procs[task_id] = proc
    # drain stderr concurrently or a chatty CLI fills the pipe and deadlocks
    stderr_task = asyncio.create_task(proc.stderr.read())
    timeout = cfg.budgets.get("timeout_s", 600)
    t0 = time.monotonic()
    steps: list[dict] = []
    data = None
    stderr_bytes = b""
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
                for step in _steps_from_event(obj):
                    step["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
                    steps.append(step)
                    if on_step:
                        try:
                            await on_step(step)
                        except Exception:
                            log.exception("on_step hook failed")
                if obj.get("type") == "result":
                    data = obj
            await proc.wait()
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise _Timeout(timeout)
    finally:
        procs.pop(task_id, None)
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        # always reap the concurrent stderr drainer, whatever way we leave
        stderr_bytes = await reap_reader(stderr_task)

    if data is None:
        stderr = stderr_bytes.decode(errors="replace")
        return {
            "status": "failed", "steps": steps,
            "error": f"no result event (exit {proc.returncode}): {stderr[:400]}",
        }
    out = {
        "stop_reason": data.get("stop_reason") or data.get("subtype"),
        "cost_usd": data.get("total_cost_usd"),
        "input_tokens": (data.get("usage") or {}).get("input_tokens"),
        "output_tokens": (data.get("usage") or {}).get("output_tokens"),
        "num_turns": data.get("num_turns"),
        "session_id": data.get("session_id"),
        "output_text": data.get("result"),
        "steps": steps,
    }
    if is_refusal(data):
        out["status"] = "refused"
    elif data.get("is_error"):
        out["status"] = "failed"
        out["error"] = str(data.get("result") or data.get("subtype"))
    else:
        out["status"] = "done"
    denied = [s for s in steps if s.get("denied")]
    if denied and out["status"] == "done":
        out["denied_tools"] = [s["summary"] for s in denied]
        # Not every denial is fatal: the daily brief is written to skip its
        # email section silently when Gmail MCP isn't granted, and does its job
        # regardless. What's fatal is a denial that stopped the run producing
        # the file its prompt names — the 2026-07-21..23 briefs, which reported
        # success having written nothing.
        blocking = [s for s in denied if denial_could_block_output(s["summary"])]
        if blocking and wrote_declared_output(text, cfg.root, wall_t0) is False:
            out["status"] = "failed"
            out["error"] = (
                "blocked by tool permissions — declared output file never "
                "written; allowedTools is missing a grant: {}".format(blocking[0]["summary"]))
        else:
            log.warning("task %s: %d tool denial(s), none fatal; "
                        "first: %s", task_id, len(denied), denied[0]["summary"])
    return out
