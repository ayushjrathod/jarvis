"""Completion-hook registry (locked decision: pluggable callbacks).

Hooks receive lifecycle events: {"event": queued|started|done|failed|refused|
cancelled, "task_id", "kind", "source", ...}. The SSE broadcaster is itself a
hook; Jarvis TTS-notify and UI push subscribe via /events without core changes.
"""

from __future__ import annotations

import inspect
import logging
from typing import Callable

log = logging.getLogger("dispatcher.hooks")


class HookRegistry:
    def __init__(self):
        self._hooks: list[Callable] = []

    def register(self, fn: Callable):
        self._hooks.append(fn)

    async def fire(self, event: dict):
        for fn in self._hooks:
            try:
                result = fn(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                log.exception("hook %s failed for event %s", fn, event.get("event"))
