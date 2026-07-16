"""Service-level tests for quick-path session continuity (claude -p --resume
within an idle window).

Same no-network rules as test_dispatcher: quick.stream is faked; nothing
invokes claude.
"""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from dispatcher.config import Config
from dispatcher.service import Service


def make_service(tmp, **cfg_over):
    cfg = Config(root=Path(tmp))
    cfg.db_path = Path(tmp) / "test.db"
    for k, v in cfg_over.items():
        setattr(cfg, k, v)
    return Service(cfg)


class RecordingStream:
    """quick.stream stand-in: records call kwargs, replays scripted metas."""

    def __init__(self, *metas):
        self.metas = list(metas)
        self.calls = []

    async def __call__(self, text, cfg, model_override=None, tools=None,
                       context="", resume_session_id=None):
        self.calls.append({"model": model_override, "resume": resume_session_id})
        meta = self.metas.pop(0)
        if meta.get("status") == "done":
            yield ("delta", "ok")
        yield ("meta", meta)


async def drain(svc, task):
    return [e async for e in svc.stream_quick(task)]


class TestQuickContinuity(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = make_service(self.tmp.name, quick_session_idle_minutes=10)

    async def asyncTearDown(self):
        await self.svc.shutdown()

    def tearDown(self):
        self.tmp.cleanup()

    async def test_followup_within_window_resumes(self):
        fake = RecordingStream({"status": "done", "session_id": "sess-1"},
                               {"status": "done", "session_id": "sess-2"})
        with patch("dispatcher.service.quick.stream", fake):
            t1 = await self.svc.create_task("what's today?", "voice", "quick")
            await drain(self.svc, t1)
            t2 = await self.svc.create_task("and tomorrow?", "voice", "quick")
            await drain(self.svc, t2)
        self.assertIsNone(fake.calls[0]["resume"])
        self.assertEqual(fake.calls[1]["resume"], "sess-1")
        # latest session id wins for the next follow-up
        self.assertEqual(self.svc.quick_sessions["voice"][0], "sess-2")

    async def test_sources_do_not_share_sessions(self):
        fake = RecordingStream({"status": "done", "session_id": "sess-v"},
                               {"status": "done", "session_id": "sess-a"})
        with patch("dispatcher.service.quick.stream", fake):
            await drain(self.svc, await self.svc.create_task("q", "voice", "quick"))
            await drain(self.svc, await self.svc.create_task("q", "api", "quick"))
        self.assertIsNone(fake.calls[1]["resume"])

    async def test_idle_expiry_starts_fresh(self):
        self.svc.quick_sessions["voice"] = ("old-sess", time.monotonic() - 601)
        fake = RecordingStream({"status": "done", "session_id": "sess-new"})
        with patch("dispatcher.service.quick.stream", fake):
            await drain(self.svc, await self.svc.create_task("q", "voice", "quick"))
        self.assertIsNone(fake.calls[0]["resume"])

    async def test_disabled_never_resumes_or_stores(self):
        svc = make_service(self.tmp.name, quick_session_idle_minutes=0)
        svc.quick_sessions["voice"] = ("old-sess", time.monotonic())
        fake = RecordingStream({"status": "done", "session_id": "sess-x"})
        with patch("dispatcher.service.quick.stream", fake):
            await drain(svc, await svc.create_task("q", "voice", "quick"))
        self.assertIsNone(fake.calls[0]["resume"])
        self.assertEqual(svc.quick_sessions["voice"][0], "old-sess")
        await svc.shutdown()

    async def test_vanished_session_retries_fresh_once(self):
        self.svc.quick_sessions["voice"] = ("gone-sess", time.monotonic())
        fake = RecordingStream(
            {"status": "failed",
             "error": "claude exited 1: No conversation found with session ID gone-sess"},
            {"status": "done", "session_id": "sess-new"},
        )
        with patch("dispatcher.service.quick.stream", fake):
            task = await self.svc.create_task("q", "voice", "quick")
            events = await drain(self.svc, task)
        self.assertEqual(fake.calls[0]["resume"], "gone-sess")
        self.assertIsNone(fake.calls[1]["resume"])
        self.assertEqual(events[-1][1]["status"], "done")
        got = self.svc.db.get_task(task["id"])
        self.assertEqual([r["status"] for r in got["runs"]], ["failed", "done"])
        self.assertEqual(self.svc.quick_sessions["voice"][0], "sess-new")


if __name__ == "__main__":
    unittest.main()
