"""Agentic headless runner: `claude -p` with guardrails, refusal fallback
(locked decision #3), and full run logging.

Guardrails per unattended run: --allowedTools (per task type, read-only
baseline), --max-budget-usd, and a wall-clock timeout. This CLI version has no
--max-turns flag; the budget cap bounds runaway loops instead. Never uses
permission-skipping flags.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re

from .config import Config

log = logging.getLogger("dispatcher.runner")

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
                   max_cost_usd: float | None = None) -> dict:
    """One `claude -p` attempt (with one internal retry on a transient
    spawn/timeout error). Returns normalized result fields.
    resume_session_id continues an earlier CLI session (reflection forks ride
    the warm prompt cache); max_cost_usd overrides the default budget cap."""
    for attempt in (1, 2):
        try:
            return await _attempt(text, cfg, model, tools, procs, task_id,
                                  system_extra, resume_session_id, max_cost_usd)
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


def build_cmd(text: str, cfg: Config, model: str | None, tools: list[str],
              system_extra: str = "", resume_session_id: str | None = None,
              max_cost_usd: float | None = None) -> list[str]:
    system = AGENT_SYSTEM + ("\n\n" + system_extra if system_extra else "")
    budget = max_cost_usd or cfg.budgets.get("max_cost_per_task_usd", 0.50)
    cmd = [
        cfg.claude_bin, "-p", text,
        "--output-format", "json",
        "--max-budget-usd", str(budget),
        "--append-system-prompt", system,
    ]
    if resume_session_id:
        cmd += ["--resume", resume_session_id]
    if tools:
        cmd += ["--allowedTools", ",".join(tools)]
    if model:
        cmd += ["--model", model]
    effort = cfg.models.get("effort")
    if effort:
        cmd += ["--effort", effort]
    return cmd


async def _attempt(text: str, cfg: Config, model: str | None, tools: list[str],
                    procs: dict, task_id: str, system_extra: str = "",
                    resume_session_id: str | None = None,
                    max_cost_usd: float | None = None) -> dict:
    cmd = build_cmd(text, cfg, model, tools, system_extra,
                    resume_session_id, max_cost_usd)
    env = dict(os.environ)
    if cfg.claude_config_dir:
        env["CLAUDE_CONFIG_DIR"] = cfg.claude_config_dir

    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cfg.root, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    procs[task_id] = proc
    timeout = cfg.budgets.get("timeout_s", 600)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise _Timeout(timeout)
    finally:
        procs.pop(task_id, None)

    data = _parse_json(stdout)
    if data is None:
        return {
            "status": "failed",
            "error": f"unparseable output (exit {proc.returncode}): "
                     f"{stderr[:400].decode(errors='replace')}",
        }
    out = {
        "stop_reason": data.get("stop_reason") or data.get("subtype"),
        "cost_usd": data.get("total_cost_usd"),
        "input_tokens": (data.get("usage") or {}).get("input_tokens"),
        "output_tokens": (data.get("usage") or {}).get("output_tokens"),
        "num_turns": data.get("num_turns"),
        "session_id": data.get("session_id"),
        "output_text": data.get("result"),
    }
    if is_refusal(data):
        out["status"] = "refused"
    elif data.get("is_error"):
        out["status"] = "failed"
        out["error"] = str(data.get("result") or data.get("subtype"))
    else:
        out["status"] = "done"
    return out


def _parse_json(stdout: bytes) -> dict | None:
    for candidate in (stdout, stdout.strip().splitlines()[-1] if stdout.strip() else b""):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            continue
    return None
