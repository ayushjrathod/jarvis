"""In-process pub/sub feeding GET /events (SSE).

Slow subscribers drop events — but never the ones that matter. Every
stream-json message of an agentic run fires a `step`, so a phone on a slow
Tailscale link sits 256 steps deep exactly when the run settles — and the
old code dropped the newest event, i.e. the `done` and the `notify`. Steps
are still droppable (the timeline just has gaps), but a lifecycle event
evicts queued steps to make room instead. Counters stay for /stats honesty.
"""

from __future__ import annotations

import asyncio

# Per-message stream noise: safe to lose, superseded by what follows it.
DROPPABLE = {"step"}


class EventBus:
    def __init__(self, max_queue: int = 256):
        self._subs: set[asyncio.Queue] = set()
        self._max = max_queue
        self.dropped_steps = 0
        self.evicted_steps = 0
        self.dropped_protected = 0

    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=self._max)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._subs.discard(q)

    def publish(self, event: dict):
        kind = event.get("event")
        for q in list(self._subs):
            try:
                q.put_nowait(event)
                continue
            except asyncio.QueueFull:
                pass
            if kind in DROPPABLE:
                self.dropped_steps += 1
                continue
            if self._evict_one_step(q):
                self.evicted_steps += 1
                try:
                    q.put_nowait(event)
                    continue
                except asyncio.QueueFull:
                    pass
            self.dropped_protected += 1

    def _evict_one_step(self, q: asyncio.Queue) -> bool:
        """Remove the oldest queued DROPPABLE event, preserving order of the
        rest. False when the queue holds no steps (full of lifecycle)."""
        buf = []
        try:
            while True:
                buf.append(q.get_nowait())
        except asyncio.QueueEmpty:
            pass
        for i, e in enumerate(buf):
            if e.get("event") in DROPPABLE:
                del buf[i]
                break
        else:
            for e in buf:
                q.put_nowait(e)
            return False
        for e in buf:
            q.put_nowait(e)
        return True
