"""In-process pub/sub feeding GET /events (SSE). Slow subscribers drop events."""

from __future__ import annotations

import asyncio


class EventBus:
    def __init__(self, max_queue: int = 256):
        self._subs: set[asyncio.Queue] = set()
        self._max = max_queue

    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=self._max)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._subs.discard(q)

    def publish(self, event: dict):
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass
