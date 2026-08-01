"""Smoke-level unit tests — stdlib unittest, no network, no claude invocation.

Run: .venv/bin/python -m unittest discover tests
"""

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from dispatcher.classifier import classify
from dispatcher.config import Config
from dispatcher.db import Database
from dispatcher.queue_watcher import _ingest_one, parse_task_file
from dispatcher import runner
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
    """Models the stream-json CLI (Phase H runner): stdout yields one result
    line then EOF; stderr reads empty. The old communicate_* kwargs are kept
    so the retry tests read the same."""

    def __init__(self, communicate_result=None, communicate_delay=0.0, returncode=0):
        body = (communicate_result or (b'{"result": "ok", "is_error": false}', b""))[0]
        obj = {"type": "result", **json.loads(body)}
        self._lines = [json.dumps(obj).encode() + b"\n"]
        self._delay = communicate_delay
        self.returncode = returncode
        self.killed = False
        self.stdout = self
        self.stderr = self

    async def readline(self):
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._lines.pop(0) if self._lines else b""

    async def read(self):
        return b""

    def kill(self):
        self.killed = True
        self._lines = []

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


class TestDeniedToolSurfacing(unittest.IsolatedAsyncioTestCase):
    """2026-07-21..23: the CLI denied the daily brief every Write, the model
    gave up and explained itself, and the result line still said success — so
    three briefs that wrote nothing settled 'done' and nobody noticed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.prompt = "Write today's brief to vault/briefs/x.md please"

    def tearDown(self):
        self.tmp.cleanup()

    def _proc(self, denial=True, content=None):
        proc = _FakeProc()
        content = content or (
            "Claude requested permissions to write to vault/briefs/x.md,"
            " but you haven't granted it yet." if denial else "wrote it")
        proc._lines.insert(0, json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "is_error": denial, "content": content},
        ]}}).encode() + b"\n")
        return proc

    async def _run(self, proc, text=None, writes_output=False):
        cfg = _test_cfg()
        cfg.root = self.root

        async def readline_writing():   # the agent writes the file mid-stream
            out = self.root / "vault" / "briefs"
            out.mkdir(parents=True, exist_ok=True)
            (out / "x.md").write_text("# brief")
            proc.readline = proc._orig_readline
            return await proc.readline()

        def spawn(*a, **kw):
            if writes_output:
                proc._orig_readline = proc.readline
                proc.readline = readline_writing
            return proc

        with patch("dispatcher.runner.asyncio.create_subprocess_exec",
                   AsyncMock(side_effect=spawn)):
            return await run_once(text or self.prompt, cfg, None, [], {}, "task1")

    async def test_denial_with_no_output_file_fails(self):
        result = await self._run(self._proc())
        self.assertEqual(result["status"], "failed")
        self.assertIn("blocked by tool permissions", result["error"])
        self.assertIn("vault/briefs/x.md", result["error"])

    async def test_denial_is_tolerated_when_the_output_was_still_written(self):
        """The live 2026-07-24 case: Gmail MCP was denied, but the brief's
        prompt says to skip email silently, and the file was written."""
        result = await self._run(self._proc(), writes_output=True)
        self.assertEqual(result["status"], "done")
        self.assertTrue(result["denied_tools"])      # recorded, not fatal

    async def test_stale_output_file_does_not_mask_a_denial(self):
        out = self.root / "vault" / "briefs"
        out.mkdir(parents=True)
        f = out / "x.md"
        f.write_text("yesterday's brief")
        os.utime(f, (time.time() - 86400, time.time() - 86400))
        result = await self._run(self._proc())
        self.assertEqual(result["status"], "failed")

    async def test_denial_without_a_declared_output_path_is_not_fatal(self):
        result = await self._run(self._proc(), text="just answer me")
        self.assertEqual(result["status"], "done")
        self.assertTrue(result["denied_tools"])

    async def test_clean_run_has_no_denied_tools(self):
        result = await self._run(self._proc(denial=False))
        self.assertEqual(result["status"], "done")
        self.assertNotIn("denied_tools", result)

    # A read-only tool cannot be why the file is missing. Live cases
    # 2026-07-23 and 2026-07-30: the brief agent asks for `mcp__gmail` while
    # the server is `mcp__claude_ai_Gmail__*`, so that denial fires on every
    # run; both days the agent then made no edit (the file was already
    # correct) and a tolerable denial was promoted to a hard failure.
    _GMAIL_DENIAL = ("ERROR: Claude requested permissions to use "
                     "mcp__claude_ai_Gmail__search_threads, but you haven't "
                     "granted it yet.")
    _WRITE_DENIAL = ("ERROR: Claude requested permissions to use Write, "
                     "but you haven't granted it yet.")

    async def test_readonly_denial_with_unwritten_output_is_not_fatal(self):
        result = await self._run(self._proc(content=self._GMAIL_DENIAL))
        self.assertEqual(result["status"], "done")
        self.assertTrue(result["denied_tools"])      # recorded, not fatal

    async def test_write_denial_with_unwritten_output_still_fails(self):
        """The session-17 regression, in the CLI's real wording."""
        result = await self._run(self._proc(content=self._WRITE_DENIAL))
        self.assertEqual(result["status"], "failed")
        self.assertIn("blocked by tool permissions", result["error"])

    async def test_mixed_denials_fail_on_the_write_one(self):
        proc = self._proc(content=self._GMAIL_DENIAL)
        proc._lines.insert(0, json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "is_error": True, "content": self._WRITE_DENIAL},
        ]}}).encode() + b"\n")
        result = await self._run(proc)
        self.assertEqual(result["status"], "failed")
        self.assertIn("Write", result["error"])


class TestDenialBlocksOutput(unittest.TestCase):
    def test_write_capable_tools_block(self):
        for tool in ("Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"):
            with self.subTest(tool=tool):
                self.assertTrue(runner.denial_could_block_output(
                    f"Claude requested permissions to use {tool}, but ..."))

    def test_read_only_tools_do_not_block(self):
        for tool in ("Read", "Glob", "Grep", "WebFetch",
                     "mcp__claude_ai_Gmail__search_threads"):
            with self.subTest(tool=tool):
                self.assertFalse(runner.denial_could_block_output(
                    f"Claude requested permissions to use {tool}, but ..."))

    def test_scoped_grant_is_matched_on_the_tool_name(self):
        self.assertTrue(runner.denial_could_block_output(
            "requested permissions to use Edit(vault/briefs/**), but ..."))

    def test_unparseable_denial_stays_fatal(self):
        """Fail loud, not silent — an unrecognised wording must not become a
        false success, which is the failure mode session 17 fixed."""
        self.assertTrue(runner.denial_could_block_output("permission denied"))
        self.assertTrue(runner.denial_could_block_output(""))



class TestReapReader(unittest.IsolatedAsyncioTestCase):
    """L2: the concurrent stderr drainer is always reaped, never orphaned."""

    async def test_returns_completed_bytes(self):
        from dispatcher.runner import reap_reader

        async def r():
            return b"stderr text"
        self.assertEqual(await reap_reader(asyncio.create_task(r())), b"stderr text")

    async def test_cancels_a_hung_reader(self):
        from dispatcher.runner import reap_reader

        async def r():
            await asyncio.sleep(100)
            return b"never"
        t = asyncio.create_task(r())
        self.assertEqual(await reap_reader(t, timeout=0.01), b"")
        self.assertTrue(t.cancelled() or t.done())


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
                         resume_session_id=None, **kwargs):
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

    async def test_cancel_midstream_not_clobbered_by_late_done(self):
        # M2: cancel() arrives while the quick turn is streaming; a subsequent
        # 'done' meta must not resurrect the task back to done.
        task = await self.svc.create_task("q", "voice", "quick")
        fake = self._fake_stream(
            ("delta", "hi"),
            ("meta", {"status": "done", "session_id": "s"}))
        with patch("dispatcher.service.quick.stream", fake):
            agen = self.svc.stream_quick(task)
            self.assertEqual((await agen.__anext__())[0], "task")
            self.assertEqual((await agen.__anext__())[0], "delta")
            self.assertTrue(await self.svc.cancel(task["id"]))  # user barges in
            rest = [e async for e in agen]
        self.assertEqual(self.svc.db.get_task(task["id"])["status"], "cancelled")
        self.assertNotIn("done", [e[0] for e in rest])  # no done event emitted


class TestTrustBoundaryMetadata(unittest.TestCase):
    """H1 pure guard: external metadata may NARROW tools/budget but never WIDEN
    them, and can never inject a resume session."""

    def test_untrusted_strips_grants_and_clamps_budget(self):
        from dispatcher.service import sanitize_untrusted_metadata
        out = sanitize_untrusted_metadata(
            {"allowed_tools": ["Bash", "Write"], "resume_session_id": "evil",
             "max_cost_usd": 100, "task_type": "summarize", "screenshot": "s.png"},
            cost_cap=3.0)
        self.assertNotIn("allowed_tools", out)
        self.assertNotIn("resume_session_id", out)
        self.assertEqual(out["max_cost_usd"], 3.0)          # clamped down to cap
        self.assertEqual(out["task_type"], "summarize")     # innocuous keys kept
        self.assertEqual(out["screenshot"], "s.png")        # screen path survives

    def test_untrusted_keeps_lower_budget(self):
        from dispatcher.service import sanitize_untrusted_metadata
        out = sanitize_untrusted_metadata({"max_cost_usd": 0.25}, cost_cap=3.0)
        self.assertEqual(out["max_cost_usd"], 0.25)         # narrowing allowed

    def test_untrusted_drops_nonnumeric_budget(self):
        from dispatcher.service import sanitize_untrusted_metadata
        self.assertNotIn("max_cost_usd",
                         sanitize_untrusted_metadata({"max_cost_usd": "lots"}, 3.0))
        self.assertNotIn("max_cost_usd",
                         sanitize_untrusted_metadata({"max_cost_usd": True}, 3.0))

    def test_none_and_empty_metadata_passthrough(self):
        from dispatcher.service import sanitize_untrusted_metadata
        self.assertIsNone(sanitize_untrusted_metadata(None, 3.0))
        self.assertEqual(sanitize_untrusted_metadata({}, 3.0), {})


class TestTrustBoundaryDispatch(unittest.IsolatedAsyncioTestCase):
    """H1 end-to-end: an untrusted /task carrying allowed_tools:[Bash] and a
    huge budget must not reach the CLI with Bash or a raised cap; a trusted
    server spawn keeps its metadata verbatim."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = Config(root=Path(self.tmp.name))
        cfg.db_path = Path(self.tmp.name) / "test.db"
        cfg.default_tools = ["Read", "Glob", "Grep"]
        cfg.budgets = {"max_cost_per_task_usd": 3.00, "timeout_s": 5}
        from dispatcher.service import Service
        self.svc = Service(cfg)

    def tearDown(self):
        self.tmp.cleanup()

    async def test_untrusted_agentic_cannot_grant_bash_or_raise_budget(self):
        from dispatcher.runner import build_cmd, grants_bash
        task = await self.svc.create_task(
            "do a thing", "api", "agentic",
            metadata={"allowed_tools": ["Bash", "Write"],
                      "max_cost_usd": 100, "resume_session_id": "evil"})
        # the row itself is stored already-sanitized
        stored = json.loads(task["metadata"])
        self.assertNotIn("allowed_tools", stored)
        self.assertNotIn("resume_session_id", stored)
        self.assertEqual(stored["max_cost_usd"], 3.00)

        captured = {}

        async def fake_run_once(text, cfg, model, tools, procs, task_id, **kw):
            captured.update(tools=tools, max_cost_usd=kw.get("max_cost_usd"),
                            resume=kw.get("resume_session_id"))
            return {"status": "done", "num_turns": 1}

        with patch("dispatcher.service.runner.run_once", fake_run_once):
            await self.svc._run_agentic_inner(task)

        # tools fell back to the configured read-only allowlist; no Bash, no resume
        self.assertEqual(captured["tools"], ["Read", "Glob", "Grep"])
        self.assertFalse(grants_bash(captured["tools"]))
        self.assertIsNone(captured["resume"])
        self.assertEqual(captured["max_cost_usd"], 3.00)

        cmd = build_cmd(task["text"], self.svc.cfg, None,
                        captured["tools"], max_cost_usd=captured["max_cost_usd"])
        self.assertIn("--disallowedTools", cmd)
        self.assertEqual(cmd[cmd.index("--disallowedTools") + 1], "Bash")
        self.assertNotIn("Bash", cmd[cmd.index("--allowedTools") + 1])
        self.assertEqual(cmd[cmd.index("--max-budget-usd") + 1], "3.0")

    async def test_trusted_spawn_passes_metadata_through(self):
        task = await self.svc.create_task(
            "reflect", "reflection", "agentic", trusted=True,
            metadata={"allowed_tools": ["Bash"], "max_cost_usd": 100,
                      "resume_session_id": "sess-x"})
        stored = json.loads(task["metadata"])
        self.assertEqual(stored["allowed_tools"], ["Bash"])
        self.assertEqual(stored["max_cost_usd"], 100)
        self.assertEqual(stored["resume_session_id"], "sess-x")


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
