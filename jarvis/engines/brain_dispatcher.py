"""Brain over the dispatcher's HTTP API (locked decision #1: the dispatcher is
the only component that invokes Claude; Jarvis is just a client).

- submit(): POST /task. SSE response → quick (ack-less stream of deltas);
  JSON 202 → agentic (speakable ack now, completion notice later).
- notices(): subscribes to GET /events and yields spoken-notice events when
  agentic tasks finish.
- cancel(): closes the in-flight stream (server kills the subprocess) and
  cancels a running agentic task — the barge-in path.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

import httpx

from jarvis.plugins.base import Brain, BrainEvent

log = logging.getLogger("jarvis.brain")


async def _sse_events(response) -> AsyncIterator[tuple[str, dict]]:
    event, data_lines = None, []
    async for line in response.aiter_lines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
        elif line == "" and event:
            try:
                data = json.loads("\n".join(data_lines)) if data_lines else {}
            except json.JSONDecodeError:
                data = {}
            yield event, data
            event, data_lines = None, []


class DispatcherBrain(Brain):
    def __init__(self, base_url: str = "http://127.0.0.1:8765"):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(10, read=300))
        self._response = None
        self._current_task_id: str | None = None

    async def submit(self, text: str, source: str = "voice") -> AsyncIterator[BrainEvent]:
        req = self._client.build_request(
            "POST", f"{self.base_url}/task", json={"text": text, "source": source}
        )
        resp = await self._client.send(req, stream=True)
        self._response = resp
        try:
            ctype = resp.headers.get("content-type", "")
            if ctype.startswith("application/json"):
                body = json.loads(await resp.aread())
                self._current_task_id = body.get("task_id")
                yield BrainEvent("ack", body.get("ack", "On it."), body.get("task_id"))
                return
            async for event, data in _sse_events(resp):
                if event == "task":
                    self._current_task_id = data.get("task_id")
                elif event == "delta":
                    yield BrainEvent("delta", data.get("text", ""), self._current_task_id)
                elif event == "done":
                    if data.get("status") != "done":
                        # the dispatcher supplies a speakable reason when it
                        # has one (e.g. "Claude's session limit is hit…")
                        why = data.get("speech") or "Sorry, that didn't work."
                        yield BrainEvent("error", why, data.get("task_id"))
                    yield BrainEvent("done", "", data.get("task_id"))
        finally:
            await resp.aclose()
            self._response = None

    async def cancel(self) -> None:
        resp, self._response = self._response, None
        if resp is not None:
            await resp.aclose()  # server-side generator dies with the connection
        if self._current_task_id:
            try:
                await self._client.post(f"{self.base_url}/task/{self._current_task_id}/cancel")
            except httpx.HTTPError:
                pass
        self._current_task_id = None

    async def notices(self) -> AsyncIterator[BrainEvent]:
        """Yields a speakable notice when any agentic task completes."""
        while True:
            try:
                async with self._client.stream(
                    "GET", f"{self.base_url}/events", timeout=httpx.Timeout(10, read=None)
                ) as resp:
                    async for event, data in _sse_events(resp):
                        if data.get("kind") != "agentic":
                            continue
                        summary = (data.get("text") or "your task")[:60]
                        if event == "done":
                            yield BrainEvent("notice", f"Done: {summary}", data.get("task_id"))
                        elif event == "failed":
                            msg = f"Task failed: {summary}."
                            if data.get("speech"):
                                msg += " " + data["speech"]
                            yield BrainEvent("notice", msg, data.get("task_id"))
            except (httpx.HTTPError, httpx.StreamError) as e:
                # StreamError is a RuntimeError, not an HTTPError — a stream
                # torn down between reads raises it and must also reconnect
                log.warning("events stream dropped (%s); reconnecting", e)
                await asyncio.sleep(3)

    async def aclose(self):
        await self._client.aclose()
