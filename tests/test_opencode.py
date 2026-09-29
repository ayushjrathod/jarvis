"""OpenCode backend (`dispatcher/opencode.py`).

Same posture as test_quick.py: stdlib unittest, fake processes replaying
scripted `opencode run --format json` NDJSON lines — no subprocess, no
network, no model. Pins the event parsing, the meta shape the service
reads, and the argv contract (agent choice, model resolution, resume).

A live-account caveat these tests do NOT cover (it needs the real API):
free-tier models reject project configs carrying `tools`/`permission`
overrides, which is why this backend scopes tools coarsely by built-in
agent (`plan` vs `build`) instead of per-tool flags. See the module doc.
"""

import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from dispatcher import opencode
from dispatcher.config import Config


def _text(body: str, session="ses-1") -> bytes:
    return json.dumps({
        "type": "text", "sessionID": session,
        "part": {"type": "text", "text": body},
    }).encode() + b"\n"


def _tool(tool="read", session="ses-1") -> bytes:
    return json.dumps({
        "type": "tool_use", "sessionID": session,
        "part": {"type": "tool", "tool": tool, "callID": "c1",
                 "state": {"status": "completed",
                           "input": {"filePath": "vault/memory/USER.md"},
                           "output": "hello"}},
    }).encode() + b"\n"


def _finish(reason="stop", session="ses-1", tokens=None, cost=None) -> bytes:
    part = {"type": "step-finish", "reason": reason}
    if tokens is not None:
        part["tokens"] = tokens
    if cost is not None:
        part["cost"] = cost
    return json.dumps({
        "type": "step_finish", "sessionID": session, "part": part,
    }).encode() + b"\n"


def _error(message: str, session="ses-1") -> bytes:
    return json.dumps({
        "type": "error", "sessionID": session,
        "error": {"message": message},
    }).encode() + b"\n"


class _FakeProc:
    """stdout replays scripted lines then EOF; stderr reads once."""

    def __init__(self, lines, stderr=b"", returncode=0):
        self._lines = list(lines)
        self._stderr = stderr
        self.returncode = returncode
        self.killed = False
        self.stdout = self
        self.stderr = self

    async def readline(self):
        return self._lines.pop(0) if self._lines else b""

    async def read(self):
        out, self._stderr = self._stderr, b""
        return out

    def kill(self):
        self.killed = True
        self._lines = []

    async def wait(self):
        return self.returncode


def _cfg(**budgets):
    cfg = Config(root=Path("."))
    cfg.opencode_bin = "opencode"
    cfg.models = {}
    cfg.budgets = {"quick_timeout_s": 5, "timeout_s": 5, **budgets}
    return cfg


async def _drain_stream(cfg, proc, **kw):
    with patch("dispatcher.opencode.asyncio.create_subprocess_exec",
               AsyncMock(return_value=proc)):
        deltas, meta = [], None
        async for kind, payload in opencode.stream("q", cfg, **kw):
            if kind == "delta":
                deltas.append(payload)
            else:
                meta = payload
    return deltas, meta


class TestStreamParsing(unittest.IsolatedAsyncioTestCase):
    async def test_text_events_stream_then_meta_settles(self):
        proc = _FakeProc([
            _text("Hello "), _text("world."),
            _finish(tokens={"input": 10, "output": 3}, cost=0),
        ])
        deltas, meta = await _drain_stream(_cfg(), proc)
        self.assertEqual(deltas, ["Hello ", "world."])
        self.assertEqual(meta["status"], "done")
        self.assertEqual(meta["backend"], "opencode")
        self.assertEqual(meta["session_id"], "ses-1")
        self.assertEqual(meta["input_tokens"], 10)
        self.assertEqual(meta["output_tokens"], 3)
        self.assertFalse(meta["retry_on_refusal"])  # no refusal signal here
        self.assertTrue(meta["streamed"])

    async def test_tool_use_stays_out_of_the_spoken_answer(self):
        proc = _FakeProc([_text("plan first."), _tool(), _finish()])
        deltas, meta = await _drain_stream(_cfg(), proc)
        self.assertEqual(deltas, ["plan first."])
        self.assertEqual(meta["status"], "done")

    async def test_garbage_lines_are_skipped_not_fatal(self):
        proc = _FakeProc([b"not json at all\n", _text("ok"), _finish()])
        deltas, meta = await _drain_stream(_cfg(), proc)
        self.assertEqual(deltas, ["ok"])
        self.assertEqual(meta["status"], "done")

    async def test_no_text_means_not_streamed_but_still_done(self):
        proc = _FakeProc([_tool(), _finish()])
        deltas, meta = await _drain_stream(_cfg(), proc)
        self.assertEqual(deltas, [])
        self.assertEqual(meta["status"], "done")
        self.assertFalse(meta["streamed"])


class TestStreamFailures(unittest.IsolatedAsyncioTestCase):
    async def test_error_event_fails_with_its_message(self):
        proc = _FakeProc([_error("OpenCode's free tier can only be used")])
        _, meta = await _drain_stream(_cfg(), proc)
        self.assertEqual(meta["status"], "failed")
        self.assertIn("free tier", meta["error"])

    async def test_no_events_reports_stderr_and_exit_code(self):
        proc = _FakeProc([], stderr=b"boom", returncode=1)
        _, meta = await _drain_stream(_cfg(), proc)
        self.assertEqual(meta["status"], "failed")
        self.assertIn("exited 1", meta["error"])
        self.assertIn("boom", meta["error"])

    async def test_timeout_kills_the_process(self):
        class Hanging(_FakeProc):
            async def readline(self):
                await asyncio.sleep(10)
        proc = Hanging([])
        _, meta = await _drain_stream(_cfg(quick_timeout_s=0.05), proc)
        self.assertEqual(meta["status"], "failed")
        self.assertIn("timeout", meta["error"])
        self.assertTrue(proc.killed)


class TestStreamProcessRegistration(unittest.IsolatedAsyncioTestCase):
    async def test_registered_while_streaming_and_cleared_after(self):
        procs = {}
        seen = []
        proc = _FakeProc([_text("hi"), _finish()])
        with patch("dispatcher.opencode.asyncio.create_subprocess_exec",
                   AsyncMock(return_value=proc)):
            async for kind, _ in opencode.stream("q", _cfg(), procs=procs,
                                                 task_id="t1"):
                seen.append(procs.get("t1") is proc)
        self.assertTrue(seen[0])
        self.assertNotIn("t1", procs)


class TestCommandLine(unittest.IsolatedAsyncioTestCase):
    async def _cmd(self, cfg, **kw):
        spawn = AsyncMock(return_value=_FakeProc([_finish()]))
        with patch("dispatcher.opencode.asyncio.create_subprocess_exec", spawn):
            async for _ in opencode.stream("ask me", cfg, **kw):
                pass
        return list(spawn.await_args.args)

    async def test_quick_rides_the_readonly_plan_agent_as_json(self):
        cmd = await self._cmd(_cfg())
        self.assertEqual(cmd[1], "run")
        self.assertIn("--format", cmd)
        self.assertEqual(cmd[cmd.index("--format") + 1], "json")
        self.assertEqual(cmd[cmd.index("--agent") + 1], "plan")

    async def test_default_free_model_when_nothing_configured(self):
        cmd = await self._cmd(_cfg())
        self.assertEqual(cmd[cmd.index("-m") + 1], opencode.DEFAULT_MODEL)

    async def test_slash_model_ids_pass_through(self):
        cfg = _cfg()
        cfg.models = {"quick": "opencode/muse-spark-1.3"}
        cmd = await self._cmd(cfg)
        self.assertEqual(cmd[cmd.index("-m") + 1],
                         "opencode/muse-spark-1.3")

    async def test_bare_claude_ids_fall_back_to_the_free_default(self):
        # a Claude Code ID would just fail on `-m` — never pass it through
        cfg = _cfg()
        cfg.models = {"quick": "claude-sonnet-5"}
        cmd = await self._cmd(cfg)
        self.assertEqual(cmd[cmd.index("-m") + 1], opencode.DEFAULT_MODEL)

    async def test_model_override_is_verbatim(self):
        cmd = await self._cmd(_cfg(), model_override="anthropic/claude-x")
        self.assertEqual(cmd[cmd.index("-m") + 1], "anthropic/claude-x")

    async def test_resume_session_passed_through(self):
        cmd = await self._cmd(_cfg(), resume_session_id="ses-9")
        self.assertEqual(cmd[cmd.index("-s") + 1], "ses-9")

    async def test_system_context_prepended_to_the_message(self):
        cmd = await self._cmd(_cfg(), context="# Area: tasks")
        message = cmd[cmd.index("run") + 1]
        self.assertIn("ask me", message)          # the user text survives
        self.assertIn("# Area: tasks", message)   # …below the persona steer
        self.assertIn("Jarvis", message)


class TestAgentChoice(unittest.TestCase):
    def test_empty_means_readonly(self):
        self.assertTrue(opencode.is_read_only(None))
        self.assertTrue(opencode.is_read_only([]))

    def test_read_trio_is_readonly(self):
        self.assertTrue(opencode.is_read_only(["Read", "Glob", "Grep"]))
        self.assertTrue(opencode.is_read_only(["Read(vault/**)"]))

    def test_anything_else_needs_the_write_agent(self):
        for tools in (["Read", "Edit(vault/**)"], ["Bash"], ["Write"],
                      ["Multiedit"], ["mcp__gmail"]):
            self.assertFalse(opencode.is_read_only(tools), tools)

    def test_agentic_cmd_picks_agent_by_grant(self):
        cfg = _cfg()
        readonly = opencode.build_agentic_cmd("t", cfg, None, ["Read", "Grep"], "")
        self.assertEqual(readonly[readonly.index("--agent") + 1], "plan")
        write = opencode.build_agentic_cmd("t", cfg, None, ["Read", "Edit"], "")
        self.assertEqual(write[write.index("--agent") + 1], "build")


class TestAgenticRunOnce(unittest.IsolatedAsyncioTestCase):
    async def _run(self, cfg, proc, **kw):
        with patch("dispatcher.opencode.asyncio.create_subprocess_exec",
                   AsyncMock(return_value=proc)):
            return await opencode.run_once("do it", cfg, None, ["Read"],
                                           {}, "task-1", **kw)

    async def test_done_carries_steps_turns_and_session(self):
        proc = _FakeProc([_text("working"), _tool(),
                          _finish(tokens={"input": 5, "output": 9})])
        out = await self._run(_cfg(), proc)
        self.assertEqual(out["status"], "done")
        self.assertEqual(out["session_id"], "ses-1")
        self.assertEqual(out["num_turns"], 0)  # no step_start legs here
        kinds = [s["type"] for s in out["steps"]]
        self.assertIn("text", kinds)
        self.assertIn("tool_use", kinds)
        self.assertIn("result", kinds)
        self.assertIn("working", out["output_text"])

    async def test_step_start_counts_as_turns(self):
        start = json.dumps({"type": "step_start",
                            "sessionID": "ses-1"}).encode() + b"\n"
        proc = _FakeProc([start, _text("hi"), _finish()])
        out = await self._run(_cfg(), proc)
        self.assertEqual(out["num_turns"], 1)

    async def test_error_event_fails(self):
        proc = _FakeProc([_error("Insufficient account funds")])
        out = await self._run(_cfg(), proc)
        self.assertEqual(out["status"], "failed")
        self.assertIn("funds", out["error"])

    async def test_spawn_error_is_failed_not_raised(self):
        with patch("dispatcher.opencode.asyncio.create_subprocess_exec",
                   AsyncMock(side_effect=OSError("nope"))):
            out = await opencode.run_once("t", _cfg(), None, [], {}, "t1")
        self.assertEqual(out["status"], "failed")
        self.assertIn("spawn error", out["error"])

    async def test_on_step_mirrors_each_new_step(self):
        seen = []

        async def hook(step):
            seen.append(step["type"])

        proc = _FakeProc([_text("a"), _tool(), _finish()])
        await self._run(_cfg(), proc, on_step=hook)
        self.assertEqual(seen, ["text", "tool_use", "result"])


if __name__ == "__main__":
    unittest.main()
