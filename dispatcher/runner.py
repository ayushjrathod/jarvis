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
    on_step is an async callback fired per timeline step as it happens."""
    for attempt in (1, 2):
        try:
            return await _attempt(text, cfg, model, tools, procs, task_id,
                                  system_extra, resume_session_id, max_cost_usd,
                                  on_step)
        except _Timeout as exc:
            if attempt == 2:
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


def grants_bash(tools: list[str] | None) -> bool:
    return any(t == "Bash" or t.startswith("Bash(") for t in tools or [])


def build_cmd(text: str, cfg: Config, model: str | None, tools: list[str],
              system_extra: str = "", resume_session_id: str | None = None,
              max_cost_usd: float | None = None) -> list[str]:
    system = AGENT_SYSTEM + ("\n\n" + system_extra if system_extra else "")
    budget = max_cost_usd or cfg.budgets.get("max_cost_per_task_usd", 0.50)
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
                prefix = "ERROR: " if block.get("is_error") else ""
                steps.append({"type": "tool_result", "summary": _clip(
                    prefix + _tool_result_text(block.get("content")))})
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
    env = dict(os.environ)
    if cfg.claude_config_dir:
        env["CLAUDE_CONFIG_DIR"] = cfg.claude_config_dir

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

    if data is None:
        try:
            stderr = (await asyncio.wait_for(stderr_task, 5)).decode(errors="replace")
        except TimeoutError:
            stderr = ""
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
    return out
