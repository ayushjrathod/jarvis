"""Quick-path streaming (locked decision #2: quick Q&A streams immediately).

One backend: `claude -p --output-format stream-json`, driven by the **Claude
Code subscription login** (`CLAUDE_CONFIG_DIR`, set in config.yaml). It yields
("delta", str) chunks followed by exactly one ("meta", dict). Refusal retry is
the dispatcher's job here (meta["retry_on_refusal"] is True).

The Anthropic Messages API backend that used to live alongside this was
**removed 2026-07-27 at the user's direction** — they are not funding an API
key, so the whole path (plus its history store, abort handle, pooled client and
unusable-key fallback) was dead weight and a live trap: an API key present in
the environment makes the `claude` CLI abandon the subscription login in favour
of it. `runner.cli_env` still strips those variables from every CLI subprocess,
which is now pure defence rather than a companion to an API path. The removed
code is recoverable from git (`ce93ea0`) if that decision ever changes.

This supersedes the "quick Q&A -> streaming Messages API" half of locked
decision #2 in CLAUDE.md; the substance of that decision (quick streams, agentic
runs headless) is unchanged, only the mechanism.
"""

from __future__ import annotations

import asyncio
import json
import logging

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

# Reported by GET /health. A constant now that there is nothing to resolve.
BACKEND = "claude_cli"


async def stream(text: str, cfg: Config, model_override: str | None = None,
                 tools: list[str] | None = None, context: str = "",
                 resume_session_id: str | None = None,
                 procs: dict | None = None, task_id: str | None = None):
    """procs/task_id let the caller register the in-flight subprocess for
    cancel/barge-in (mirrors runner.run_once): while a turn is streaming,
    procs[task_id] holds the live process so POST /task/{id}/cancel can kill it."""
    async for item in _stream_cli(text, cfg, model_override, tools, context,
                                  resume_session_id, procs, task_id):
        yield item


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
                    # `streamed` tells the caller whether any INCREMENTAL text
                    # arrived. When it didn't, the single delta below is
                    # synthesized from the final result, so there is no real
                    # time-to-first-token to record (see telemetry.stream_stats).
                    meta["streamed"] = saw_delta
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
