"""Smoke-level unit tests — stdlib unittest, no network, no claude invocation.

Run: .venv/bin/python -m unittest discover tests
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from dispatcher.classifier import classify
from dispatcher.config import Config
from dispatcher.db import Database
from dispatcher.queue_watcher import _ingest_one, parse_task_file
from dispatcher.runner import is_refusal, run_once
from dispatcher.service import make_ack


class TestClassifier(unittest.TestCase):
    def test_quick_questions(self):
        for text in [
            "what's the capital of France?",
            "How tall is the Eiffel Tower",
            "is it going to rain today?",
            "explain systemd user timers",
            "hello there",
        ]:
            self.assertEqual(classify(text), "quick", text)

    def test_agentic_tasks(self):
        for text in [
            "summarize the files in vault/notes into vault/briefs/test.md",
            "create a task file for renewing the domain",
            "organize my notes from this week",
            "write a summary of the meeting and save it",
            "review everything in the notes/ folder",
        ]:
            self.assertEqual(classify(text), "agentic", text)

    def test_path_reference_wins_over_question_shape(self):
        self.assertEqual(classify("what is in vault/notes/ideas.md?"), "agentic")


class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_task_lifecycle(self):
        task = self.db.create_task("do a thing", "api", "agentic", metadata={"x": 1})
        self.assertEqual(task["status"], "queued")
        self.db.set_task_status(task["id"], "running")
        got = self.db.get_task(task["id"])
        self.assertEqual(got["status"], "running")
        self.assertEqual(got["runs"], [])
        self.assertEqual(len(self.db.list_tasks()), 1)

    def test_refusal_two_run_flow(self):
        task = self.db.create_task("borderline task", "queue", "agentic")
        r1 = self.db.create_run(task["id"], 1, "claude-fable-5")
        self.db.finish_run(r1, "refused", stop_reason="refusal")
        r2 = self.db.create_run(task["id"], 2, "claude-opus-4-8")
        self.db.finish_run(r2, "done", cost_usd=0.03, output_text="ok")
        self.db.set_task_status(task["id"], "done")

        got = self.db.get_task(task["id"])
        self.assertEqual(got["status"], "done")
        self.assertEqual([r["status"] for r in got["runs"]], ["refused", "done"])
        self.assertEqual(got["runs"][1]["model"], "claude-opus-4-8")
        self.assertAlmostEqual(got["runs"][1]["cost_usd"], 0.03)

    def test_cancel_open_runs(self):
        task = self.db.create_task("t", "api", "agentic")
        self.db.create_run(task["id"], 1, "m")
        self.db.cancel_open_runs(task["id"])
        got = self.db.get_task(task["id"])
        self.assertEqual(got["runs"][0]["status"], "cancelled")
        self.assertIsNotNone(got["runs"][0]["finished_at"])

    def test_reconcile_orphans(self):
        stuck = self.db.create_task("interrupted", "timer", "agentic")
        self.db.set_task_status(stuck["id"], "running")
        self.db.create_run(stuck["id"], 1, "m")
        queued = self.db.create_task("never started", "queue", "agentic")
        finished = self.db.create_task("fine", "api", "quick")
        self.db.set_task_status(finished["id"], "done")

        self.assertEqual(self.db.reconcile_orphans(), 2)

        self.assertEqual(self.db.get_task(stuck["id"])["status"], "failed")
        run = self.db.get_task(stuck["id"])["runs"][0]
        self.assertEqual(run["status"], "failed")
        self.assertIn("interrupted", run["error"])
        self.assertIsNotNone(run["finished_at"])
        self.assertEqual(self.db.get_task(queued["id"])["status"], "failed")
        self.assertEqual(self.db.get_task(finished["id"])["status"], "done")
        self.assertEqual(self.db.reconcile_orphans(), 0)  # idempotent


class TestQueueParsing(unittest.TestCase):
    def test_frontmatter(self):
        meta, body = parse_task_file("---\nmode: agentic\narea: tasks\n---\nDo the thing.\n")
        self.assertEqual(meta, {"mode": "agentic", "area": "tasks"})
        self.assertEqual(body, "Do the thing.")

    def test_no_frontmatter(self):
        meta, body = parse_task_file("Just a plain task\n")
        self.assertEqual(meta, {})
        self.assertEqual(body, "Just a plain task")

    def test_body_containing_dashes(self):
        meta, body = parse_task_file("---\nmode: auto\n---\nline one\n---\nline two")
        self.assertEqual(meta["mode"], "auto")
        self.assertIn("---", body)


class TestRefusalDetection(unittest.TestCase):
    def test_variants(self):
        self.assertTrue(is_refusal({"stop_reason": "refusal"}))
        self.assertTrue(is_refusal({"subtype": "error_refusal"}))
        self.assertTrue(is_refusal({"message": {"stop_reason": "refusal"}}))
        self.assertFalse(is_refusal({"subtype": "success", "stop_reason": "end_turn"}))
        self.assertFalse(is_refusal({}))


class TestAck(unittest.TestCase):
    def test_truncates(self):
        ack = make_ack("summarize the files in vault notes into a brief please")
        self.assertTrue(ack.startswith("On it — summarize the files"))
        self.assertTrue(ack.endswith("…"))


class _FakeService:
    def __init__(self, submit_effect):
        # submit_effect: either an exception instance to raise, or a dict to return
        self.submit_effect = submit_effect
        self.calls = 0

    async def submit(self, **kwargs):
        self.calls += 1
        if isinstance(self.submit_effect, Exception):
            raise self.submit_effect
        return self.submit_effect


class TestQueueIngestRetry(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.qdir = Path(self.tmp.name)
        self.processed = self.qdir / ".processed"
        self.failed = self.qdir / ".failed"
        self.processed.mkdir()
        self.failed.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name, body="Do the thing.\n"):
        f = self.qdir / name
        f.write_text(body)
        return f

    async def test_terminal_error_moves_to_failed_immediately(self):
        f = self._write("bad.md", body="")  # empty body -> ValueError, terminal
        service = _FakeService(submit_effect={"id": "t1", "kind": "agentic"})
        retries: dict = {}
        await _ingest_one(f, service, self.processed, self.failed, retries, max_retries=3)
        self.assertFalse(f.exists())
        self.assertTrue((self.failed / "bad.md").exists())
        self.assertEqual(retries, {})

    async def test_transient_error_retries_in_place(self):
        f = self._write("t.md")
        service = _FakeService(submit_effect=OSError("disk hiccup"))
        retries: dict = {}
        await _ingest_one(f, service, self.processed, self.failed, retries, max_retries=3)
        # left in place for next poll, not moved to .failed
        self.assertTrue(f.exists())
        self.assertFalse((self.failed / "t.md").exists())
        self.assertEqual(retries["t.md"], 1)

    async def test_transient_error_exhausts_retries_then_fails(self):
        f = self._write("t.md")
        service = _FakeService(submit_effect=OSError("disk hiccup"))
        retries: dict = {}
        for _ in range(3):
            await _ingest_one(f, service, self.processed, self.failed, retries, max_retries=3)
        self.assertTrue(f.exists())  # still in place after exactly max_retries attempts
        await _ingest_one(f, service, self.processed, self.failed, retries, max_retries=3)
        self.assertFalse(f.exists())
        self.assertTrue((self.failed / "t.md").exists())
        self.assertEqual(retries, {})

    async def test_success_after_prior_transient_failure_clears_retry_count(self):
        f = self._write("t.md")
        service = _FakeService(submit_effect=OSError("disk hiccup"))
        retries: dict = {}
        await _ingest_one(f, service, self.processed, self.failed, retries, max_retries=3)
        self.assertEqual(retries["t.md"], 1)

        service.submit_effect = {"id": "t1", "kind": "agentic"}
        await _ingest_one(f, service, self.processed, self.failed, retries, max_retries=3)
        self.assertFalse(f.exists())
        self.assertTrue((self.processed / "t.t1.md").exists())
        self.assertEqual(retries, {})


class _FakeProc:
    def __init__(self, communicate_result=None, communicate_delay=0.0, returncode=0):
        self._result = communicate_result or (b'{"result": "ok", "is_error": false}', b"")
        self._delay = communicate_delay
        self.returncode = returncode
        self.killed = False

    async def communicate(self):
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._result

    def kill(self):
        self.killed = True

    async def wait(self):
        return None


def _test_cfg(**budgets):
    cfg = Config(root=Path("."))
    cfg.claude_bin = "claude"
    cfg.budgets = {"timeout_s": 5, "transient_retry_delay_s": 0, **budgets}
    return cfg


class TestRunnerTransientRetry(unittest.IsolatedAsyncioTestCase):
    async def test_spawn_error_then_success_retries_once(self):
        cfg = _test_cfg()
        good_proc = _FakeProc()
        with patch(
            "dispatcher.runner.asyncio.create_subprocess_exec",
            AsyncMock(side_effect=[OSError("no such file"), good_proc]),
        ) as mock_spawn:
            result = await run_once("do it", cfg, None, [], {}, "task1")
        self.assertEqual(result["status"], "done")
        self.assertEqual(mock_spawn.call_count, 2)

    async def test_spawn_error_twice_fails_after_one_retry(self):
        cfg = _test_cfg()
        with patch(
            "dispatcher.runner.asyncio.create_subprocess_exec",
            AsyncMock(side_effect=[OSError("a"), OSError("b")]),
        ) as mock_spawn:
            result = await run_once("do it", cfg, None, [], {}, "task1")
        self.assertEqual(result["status"], "failed")
        self.assertIn("spawn error", result["error"])
        self.assertEqual(mock_spawn.call_count, 2)

    async def test_timeout_then_success_retries_once(self):
        cfg = _test_cfg(timeout_s=0.05)
        slow_proc = _FakeProc(communicate_delay=1.0)
        good_proc = _FakeProc()
        with patch(
            "dispatcher.runner.asyncio.create_subprocess_exec",
            AsyncMock(side_effect=[slow_proc, good_proc]),
        ) as mock_spawn:
            result = await run_once("do it", cfg, None, [], {}, "task1")
        self.assertEqual(result["status"], "done")
        self.assertEqual(mock_spawn.call_count, 2)
        self.assertTrue(slow_proc.killed)

    async def test_timeout_twice_returns_timeout_status(self):
        cfg = _test_cfg(timeout_s=0.05)
        slow_proc_1 = _FakeProc(communicate_delay=1.0)
        slow_proc_2 = _FakeProc(communicate_delay=1.0)
        with patch(
            "dispatcher.runner.asyncio.create_subprocess_exec",
            AsyncMock(side_effect=[slow_proc_1, slow_proc_2]),
        ) as mock_spawn:
            result = await run_once("do it", cfg, None, [], {}, "task1")
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(mock_spawn.call_count, 2)

    async def test_refusal_is_not_retried(self):
        cfg = _test_cfg()
        refusal_proc = _FakeProc(
            communicate_result=(b'{"stop_reason": "refusal"}', b"")
        )
        with patch(
            "dispatcher.runner.asyncio.create_subprocess_exec",
            AsyncMock(side_effect=[refusal_proc]),
        ) as mock_spawn:
            result = await run_once("do it", cfg, None, [], {}, "task1")
        self.assertEqual(result["status"], "refused")
        self.assertEqual(mock_spawn.call_count, 1)

    async def test_budget_error_is_not_retried(self):
        cfg = _test_cfg()
        budget_proc = _FakeProc(
            communicate_result=(b'{"is_error": true, "subtype": "error_max_budget_usd"}', b"")
        )
        with patch(
            "dispatcher.runner.asyncio.create_subprocess_exec",
            AsyncMock(side_effect=[budget_proc]),
        ) as mock_spawn:
            result = await run_once("do it", cfg, None, [], {}, "task1")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(mock_spawn.call_count, 1)


class TestStreamQuickBookkeeping(unittest.IsolatedAsyncioTestCase):
    """stream_quick must settle task/run rows on every exit path — including
    the client vanishing mid-stream (barge-in closes the SSE connection)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = Config(root=Path(self.tmp.name))
        cfg.db_path = Path(self.tmp.name) / "test.db"
        from dispatcher.service import Service
        self.svc = Service(cfg)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _fake_stream(*events):
        async def stream(text, cfg, model_override=None, tools=None, context="",
                         resume_session_id=None):
            for ev in events:
                yield ev
        return stream

    async def test_completed_stream_marks_done(self):
        task = await self.svc.create_task("q", "voice", "quick")
        fake = self._fake_stream(
            ("delta", "hi "), ("delta", "there"),
            ("meta", {"status": "done", "cost_usd": 0.01}),
        )
        with patch("dispatcher.service.quick.stream", fake):
            events = [e async for e in self.svc.stream_quick(task)]
        self.assertEqual(events[-1][0], "done")
        got = self.svc.db.get_task(task["id"])
        self.assertEqual(got["status"], "done")
        self.assertEqual(got["runs"][0]["status"], "done")
        self.assertEqual(got["runs"][0]["output_text"], "hi there")

    async def test_client_disconnect_settles_rows_as_cancelled(self):
        task = await self.svc.create_task("q", "voice", "quick")
        fake = self._fake_stream(("delta", "hi "), ("delta", "never consumed"))
        with patch("dispatcher.service.quick.stream", fake):
            agen = self.svc.stream_quick(task)
            self.assertEqual((await agen.__anext__())[0], "task")
            self.assertEqual((await agen.__anext__())[0], "delta")
            await agen.aclose()  # what a dropped SSE connection does
        got = self.svc.db.get_task(task["id"])
        self.assertEqual(got["status"], "cancelled")
        self.assertEqual(got["runs"][0]["status"], "cancelled")
        self.assertIn("disconnected", got["runs"][0]["error"])


class TestVaultTolerance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name)
        (self.vault / "tasks").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_one_bad_file_does_not_break_listing(self):
        from dispatcher import vault
        (self.vault / "tasks" / "good.md").write_text("---\ntitle: Good\nstatus: open\n---\nok\n")
        (self.vault / "tasks" / "bad.md").write_text("---\ntitle: [unclosed\n---\nbody\n")
        tasks = vault.list_tasks(self.vault)
        self.assertEqual([t["title"] for t in tasks], ["Good"])


if __name__ == "__main__":
    unittest.main()
