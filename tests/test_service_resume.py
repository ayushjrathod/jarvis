"""Service-level tests for quick-path session continuity (claude -p --resume
within an idle window) and usage-limit fallback wiring.

Same no-network rules as test_dispatcher: quick.stream and runner.run_once are
faked; nothing invokes claude.
"""

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from dispatcher.config import Config
from dispatcher.service import Service

SESSION_MSG = "You've hit your session limit · resets 2:30pm (Asia/Kolkata)"
MODEL_MSG = ("You've reached your Fable 5 limit. Run /usage-credits to "
             "continue or switch models with /model.")


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


class TestQuickLimitFallback(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = make_service(self.tmp.name)

    async def asyncTearDown(self):
        await self.svc.shutdown()

    def tearDown(self):
        self.tmp.cleanup()

    async def test_model_limit_retries_on_fallback(self):
        fake = RecordingStream({"status": "failed", "error": MODEL_MSG},
                               {"status": "done", "session_id": "s"})
        with patch("dispatcher.service.quick.stream", fake):
            task = await self.svc.create_task("q", "voice", "quick")
            events = await drain(self.svc, task)
        self.assertEqual(fake.calls[1]["model"], "claude-opus-4-8")
        self.assertEqual(events[-1][1]["status"], "done")
        got = self.svc.db.get_task(task["id"])
        self.assertEqual([r["status"] for r in got["runs"]], ["failed", "done"])

    async def test_session_limit_fails_once_with_speech(self):
        fake = RecordingStream({"status": "failed", "error": SESSION_MSG})
        with patch("dispatcher.service.quick.stream", fake):
            task = await self.svc.create_task("q", "voice", "quick")
            events = await drain(self.svc, task)
        self.assertEqual(len(fake.calls), 1)  # no pointless fallback attempt
        done = events[-1][1]
        self.assertEqual(done["status"], "failed")
        self.assertIn("session limit", done["speech"])
        self.assertIn("2:30pm", done["speech"])

    async def test_budget_failure_gets_no_limit_treatment(self):
        fake = RecordingStream(
            {"status": "failed", "error": "Budget limit exceeded your max of $0.10"})
        with patch("dispatcher.service.quick.stream", fake):
            task = await self.svc.create_task("q", "voice", "quick")
            events = await drain(self.svc, task)
        self.assertEqual(len(fake.calls), 1)
        self.assertIsNone(events[-1][1].get("speech"))


class TestAgenticLimitHandling(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = make_service(self.tmp.name)

    async def asyncTearDown(self):
        await self.svc.shutdown()

    def tearDown(self):
        self.tmp.cleanup()

    async def test_model_limit_falls_back_like_refusal(self):
        results = [{"status": "failed", "error": MODEL_MSG}, {"status": "done"}]

        async def fake_run(*a, **kw):
            return results.pop(0)

        with patch("dispatcher.service.runner.run_once", fake_run):
            task = await self.svc.create_task("do the thing", "api", "agentic")
            await self.svc._run_agentic_inner(task)
        got = self.svc.db.get_task(task["id"])
        self.assertEqual(got["status"], "done")
        self.assertEqual([r["status"] for r in got["runs"]], ["failed", "done"])

    async def test_session_limit_fails_with_speech_event(self):
        seen = []

        async def fake_run(*a, **kw):
            return {"status": "failed", "error": SESSION_MSG}

        self.svc.hooks.register(lambda p: seen.append(p))
        with patch("dispatcher.service.runner.run_once", fake_run):
            task = await self.svc.create_task("daily brief", "api", "agentic")
            await self.svc._run_agentic_inner(task)
        got = self.svc.db.get_task(task["id"])
        self.assertEqual(got["status"], "failed")
        self.assertEqual(len(got["runs"]), 1)  # session-wide: no fallback try
        failed = [p for p in seen if p["event"] == "failed"]
        self.assertIn("session limit", failed[0]["speech"])

    async def test_timer_session_limit_requeues_after_reset(self):
        results = [{"status": "failed", "error": SESSION_MSG}, {"status": "done"}]
        seen = []

        async def fake_run(*a, **kw):
            return results.pop(0)

        self.svc.hooks.register(lambda p: seen.append(p))
        with patch("dispatcher.service.runner.run_once", fake_run), \
             patch.object(Service, "_limit_requeue_delay", return_value=0.05):
            task = await self.svc.create_task("daily brief", "timer", "agentic")
            await self.svc._run_agentic_inner(task)
            requeued = [p for p in seen if p["event"] == "requeued"]
            self.assertEqual(len(requeued), 1)
            failed = [p for p in seen if p["event"] == "failed"]
            self.assertIn("retry the task", failed[0]["speech"])
            await asyncio.sleep(0.4)  # let the requeue fire and the retry run
        retries = [t for t in self.svc.db.list_tasks()
                   if t["id"] != task["id"] and t["text"] == "daily brief"]
        self.assertEqual(len(retries), 1)
        self.assertEqual(retries[0]["status"], "done")
        self.assertIn('"limit_requeues": 1', retries[0]["metadata"])

    async def test_requeue_capped_after_two_attempts(self):
        async def fake_run(*a, **kw):
            return {"status": "failed", "error": SESSION_MSG}

        seen = []
        self.svc.hooks.register(lambda p: seen.append(p))
        with patch("dispatcher.service.runner.run_once", fake_run):
            task = await self.svc.create_task(
                "daily brief", "timer", "agentic",
                metadata={"limit_requeues": 2})
            await self.svc._run_agentic_inner(task)
        self.assertEqual([p for p in seen if p["event"] == "requeued"], [])
        self.assertNotIn(f"requeue:{task['id']}", self.svc.bg)

    async def test_non_timer_session_limit_does_not_requeue(self):
        async def fake_run(*a, **kw):
            return {"status": "failed", "error": SESSION_MSG}

        seen = []
        self.svc.hooks.register(lambda p: seen.append(p))
        with patch("dispatcher.service.runner.run_once", fake_run):
            task = await self.svc.create_task("do the thing", "api", "agentic")
            await self.svc._run_agentic_inner(task)
        self.assertEqual([p for p in seen if p["event"] == "requeued"], [])
        self.assertEqual(self.svc.db.get_task(task["id"])["status"], "failed")


if __name__ == "__main__":
    unittest.main()
