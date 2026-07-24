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
            # a valid-but-scalar payload (`data: null` / `data: 42`) parses fine
            # yet has no .get(); downstream would AttributeError outside the
            # reconnect catch and kill notices_loop (→ whole process). (L8)
            if not isinstance(data, dict):
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
            # Branch on status BEFORE content-type: FastAPI errors (500
            # {"detail":…}, 422) are application/json too, so the old
            # content-type check spoke "On it." to a failure and then did
            # nothing (M7). A non-JSON error body yielded dead silence.
            if not (200 <= resp.status_code < 300):
                yield BrainEvent(
                    "error", "Sorry, the dispatcher returned an error.", None
                )
                return
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
            # Only the actively-streaming (or just-acked) response is
            # barge-cancellable. Once submit() returns this id must not linger:
            # otherwise a later barge-in over an unrelated spoken notice would
            # POST /cancel to a still-running background agentic task (H7).
            self._current_task_id = None

    async def cancel(self) -> None:
        resp, self._response = self._response, None
        # Snapshot the id before any await so a concurrent submit() finally
        # (fired when closing resp tears down its stream) can't clear it out
        # from under us and skip cancelling the genuinely-live stream.
        task_id, self._current_task_id = self._current_task_id, None
        if resp is not None:
            await resp.aclose()  # server-side generator dies with the connection
        if task_id:
            try:
                await self._client.post(f"{self.base_url}/task/{task_id}/cancel")
            except httpx.HTTPError:
                pass

    async def notices(self) -> AsyncIterator[BrainEvent]:
        """Yields a speakable notice when a task completes worth mentioning.
        surface=False events are suppressed server-side (internal meta-work,
        or the notify gate took over); 'notify' events carry the gate's own
        spoken summary of an automation/timer result."""
        while True:
            try:
                async with self._client.stream(
                    "GET", f"{self.base_url}/events", timeout=httpx.Timeout(10, read=None)
                ) as resp:
                    # A finite non-2xx (e.g. 404 from an old/mismatched
                    # dispatcher) ends the stream WITHOUT an httpx exception, so
                    # without this the loop reconnects tight with no backoff and
                    # no log. raise_for_status() turns it into an HTTPStatusError
                    # caught below → the backoff sleep runs. (L7)
                    resp.raise_for_status()
                    async for event, data in _sse_events(resp):
                        if event == "notify" and data.get("speech"):
                            yield BrainEvent("notice", data["speech"], data.get("task_id"))
                            continue
                        if data.get("kind") != "agentic":
                            continue
                        if data.get("surface") is False:
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
            # Unconditional backoff at the loop bottom: a stream that ends
            # cleanly (EOF, no exception) must also wait before reconnecting so
            # a mismatched dispatcher can't be hammered socket-tight. (L7)
            await asyncio.sleep(3)

    async def aclose(self):
        await self._client.aclose()
