"""Notify-or-not gate (Phase I): after a background task finishes, a cheap
quick-path judgment decides whether the result is worth surfacing (spoken
notice + desktop notification) or only worth logging.

Gate idea after khoj's automations notify check (AGPL-3.0 — pattern
re-implemented, no code copied). The gate prompt is self-contained: it inlines
the request and the (clipped) result, so the run needs no session continuity.
It ran with `--resume` on the settled task's session until 2026-07-26, on the
theory that a warm cache made it nearly free; live data said otherwise
(12 runs averaging $0.141 — resuming a long agentic transcript costs more than
it saves), so it now runs fresh on `models.meta`. Failure policy is fail-open:
the user asked to be told, so a broken gate notifies with a generic summary
rather than silently dropping results.
"""

from __future__ import annotations

import asyncio
import logging
import shutil

log = logging.getLogger("dispatcher.notify")

# Meta-work is never announced to notice clients: reflections and consolidation
# are internal housekeeping, and the gate/parse runs are plumbing. (Before
# Phase I, Jarvis announced "Done: You just completed a task…" for reflections
# whenever the voice service was up.)
NEVER_SURFACE_TASK_TYPES = {"reflection", "memory-consolidate",
                            "notify-gate", "automation-parse", "media-parse",
                            "graph-extract", "graph-reconcile"}

# ...but a scheduled background job that FAILS must not fail silently — a broken
# 02:30 consolidation should be visible, not swallowed like a routine success
# (M6). Kept narrow: the quick plumbing tasks (gate/parse) handle their own
# errors and would only add noise.
SURFACE_ON_FAILURE = {"memory-consolidate"}

RESULT_CLIP = 1500

GATE_PROMPT = """\
You just finished a background task for the user. Decide whether the result \
is worth interrupting them about, or is routine/empty and should only be logged.

Task: {request}
Result (may be truncated): {result}

Interrupt-worthy: new information, something that needs action, anything the \
user explicitly asked to be told. Not worth it: routine success with nothing \
new, empty results, internal maintenance.

Reply with exactly ONE line, nothing else:
NOTIFY: <one spoken sentence with the essence of the result, max 25 words>
or
SKIP: <short reason>"""


def gate_prompt(request: str, result: str | None) -> str:
    result = (result or "").strip() or "(no text output)"
    if len(result) > RESULT_CLIP:
        result = result[:RESULT_CLIP] + "…"
    return GATE_PROMPT.format(request=request[:300], result=result)


def parse_gate(answer: str) -> tuple[str, str]:
    """("notify"|"skip", text). Malformed output fails open: ("notify", "")
    and the caller substitutes a generic summary."""
    for line in (answer or "").splitlines():
        line = line.strip()
        if line.upper().startswith("NOTIFY:"):
            return "notify", line[7:].strip()
        if line.upper().startswith("SKIP:"):
            return "skip", line[5:].strip()
    return "notify", ""


def surfacing(acfg: dict, task: dict, meta: dict, final: str) -> str:
    """Pure policy: what happens to this settled task's completion event.
    "surface" — announce as before; "silent" — suppress, no gate;
    "gate" — suppress the plain event, an LLM judgment will decide."""
    tt = meta.get("task_type")
    if tt in NEVER_SURFACE_TASK_TYPES:
        if final != "done" and tt in SURFACE_ON_FAILURE:
            return "surface"   # a broken nightly job must be visible
        return "silent"
    if not acfg.get("enabled"):
        return "surface"
    if task["source"] not in acfg.get("notify_sources", ["automation", "timer"]):
        return "surface"
    if final != "done":
        return "surface"    # failures keep their speakable error path
    if meta.get("notify", True) is False:
        return "silent"     # the user asked this automation to stay quiet
    if not acfg.get("notify_gate", True):
        return "surface"
    return "gate"


async def send_desktop(summary: str, title: str = "Jarvis") -> bool:
    """Best-effort desktop notification; False when notify-send is missing
    or fails (the SSE notify event is the primary channel)."""
    exe = shutil.which("notify-send")
    if not exe:
        log.info("notify-send not installed; desktop notification skipped")
        return False
    try:
        proc = await asyncio.create_subprocess_exec(exe, title, summary[:400])
        await asyncio.wait_for(proc.wait(), 10)
        return proc.returncode == 0
    except (OSError, TimeoutError):
        log.warning("notify-send failed", exc_info=True)
        return False
