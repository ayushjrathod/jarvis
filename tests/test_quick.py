"""Quick-path CLI backend (`dispatcher/quick.py`).

Every other test in the suite patches `dispatcher.service.quick.stream`, which
left `_stream_cli` — the code that actually parses the `claude -p` stream-json
protocol — with no coverage at all. That is the wrong thing to leave untested:
it is the highest-churn contract in the codebase (a CLI release changes the
wire format, not our code), and its agentic sibling `runner._attempt` has had
fake-process tests since Phase H.

stdlib unittest. No subprocess, no network, no model — a fake process feeds
scripted stream-json lines exactly as the CLI emits them.
"""

import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from dispatcher import quick
from dispatcher.config import Config


def _delta(text: str) -> bytes:
    """One `content_block_delta` line, the shape --include-partial-messages emits."""
    return json.dumps({
        "type": "stream_event",
        "event": {"type": "content_block_delta",
                  "delta": {"type": "text_delta", "text": text}},
    }).encode() + b"\n"


def _result(**fields) -> bytes:
    return json.dumps({"type": "result", **fields}).encode() + b"\n"


class _FakeProc:
    """stdout replays scripted lines then EOF; stderr reads once, like a pipe."""

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
    cfg.claude_bin = "claude"
    cfg.models = {"quick": "claude-sonnet-5"}
    cfg.budgets = {"quick_timeout_s": 5, "quick_max_cost_usd": 1.0, **budgets}
    return cfg


async def _drain(cfg, proc, **kw):
    """Run the backend against a fake process → (deltas, meta)."""
    with patch("dispatcher.quick.asyncio.create_subprocess_exec",
               AsyncMock(return_value=proc)):
        deltas, meta = [], None
        async for kind, payload in quick.stream("q", cfg, **kw):
            if kind == "delta":
                deltas.append(payload)
            else:
                meta = payload
    return deltas, meta


class TestStreamParsing(unittest.IsolatedAsyncioTestCase):
    async def test_partial_deltas_stream_then_result_settles(self):
        proc = _FakeProc([
            _delta("Hello "), _delta("world."),
            _result(subtype="success", is_error=False, total_cost_usd=0.04,
                    session_id="sess-1", usage={"input_tokens": 10,
                                                "output_tokens": 3}),
        ])
        deltas, meta = await _drain(_cfg(), proc)
        self.assertEqual(deltas, ["Hello ", "world."])
        self.assertEqual(meta["status"], "done")
        self.assertEqual(meta["session_id"], "sess-1")
        self.assertEqual(meta["cost_usd"], 0.04)
        self.assertEqual(meta["output_tokens"], 3)

    async def test_result_text_is_emitted_when_no_deltas_arrived(self):
        # a fast/short turn can finish without any partial-message events;
        # without this the user would get an empty answer and a 'done'
        proc = _FakeProc([_result(subtype="success", is_error=False,
                                  result="the whole answer")])
        deltas, meta = await _drain(_cfg(), proc)
        self.assertEqual(deltas, ["the whole answer"])
        self.assertEqual(meta["status"], "done")

    async def test_result_text_not_duplicated_after_deltas(self):
        proc = _FakeProc([_delta("streamed"),
                          _result(subtype="success", is_error=False,
                                  result="streamed")])
        deltas, _ = await _drain(_cfg(), proc)
        self.assertEqual(deltas, ["streamed"])          # not twice

    async def test_garbage_lines_are_skipped_not_fatal(self):
        proc = _FakeProc([b"not json at all\n", _delta("ok"),
                          _result(subtype="success", is_error=False)])
        deltas, meta = await _drain(_cfg(), proc)
        self.assertEqual(deltas, ["ok"])
        self.assertEqual(meta["status"], "done")


class TestFailureShapes(unittest.IsolatedAsyncioTestCase):
    async def test_refusal_asks_the_caller_to_retry(self):
        proc = _FakeProc([_result(subtype="refusal", is_error=False)])
        _, meta = await _drain(_cfg(), proc)
        self.assertEqual(meta["status"], "refused")
        self.assertTrue(meta["retry_on_refusal"])       # service climbs to fallback

    async def test_error_message_from_the_errors_array(self):
        # a dead --resume session reports only in `errors`, never in `result`;
        # service.is_resume_error reads this string to decide on a fresh retry
        proc = _FakeProc([_result(
            subtype="error", is_error=True,
            errors=["No conversation found with session ID: abc"])])
        _, meta = await _drain(_cfg(), proc, resume_session_id="abc")
        self.assertEqual(meta["status"], "failed")
        self.assertIn("No conversation found", meta["error"])

    async def test_error_message_prefers_result_when_present(self):
        proc = _FakeProc([_result(subtype="error", is_error=True,
                                  result="budget exceeded", errors=["ignored"])])
        _, meta = await _drain(_cfg(), proc)
        self.assertEqual(meta["error"], "budget exceeded")

    async def test_no_result_event_reports_stderr_and_exit_code(self):
        proc = _FakeProc([], stderr=b"claude: command failed", returncode=2)
        _, meta = await _drain(_cfg(), proc)
        self.assertEqual(meta["status"], "failed")
        self.assertIn("exited 2", meta["error"])
        self.assertIn("command failed", meta["error"])

    async def test_timeout_kills_the_process(self):
        class Hanging(_FakeProc):
            async def readline(self):
                await asyncio.sleep(10)
        proc = Hanging([])
        _, meta = await _drain(_cfg(quick_timeout_s=0.05), proc)
        self.assertEqual(meta["status"], "failed")
        self.assertIn("timeout", meta["error"])
        self.assertTrue(proc.killed)


class TestProcessRegistration(unittest.IsolatedAsyncioTestCase):
    """procs[task_id] is what POST /task/{id}/cancel and voice barge-in kill."""

    async def test_registered_while_streaming_and_cleared_after(self):
        procs = {}
        seen = []
        proc = _FakeProc([_delta("hi"),
                          _result(subtype="success", is_error=False)])
        with patch("dispatcher.quick.asyncio.create_subprocess_exec",
                   AsyncMock(return_value=proc)):
            async for kind, _ in quick.stream("q", _cfg(), procs=procs,
                                              task_id="t1"):
                seen.append(procs.get("t1") is proc)
        self.assertTrue(seen[0])          # live during the stream
        self.assertNotIn("t1", procs)     # released on the way out


class TestCommandLine(unittest.IsolatedAsyncioTestCase):
    """The flags are the guardrails — same contract runner.build_cmd pins."""

    async def _cmd(self, cfg, **kw):
        spawn = AsyncMock(return_value=_FakeProc(
            [_result(subtype="success", is_error=False)]))
        with patch("dispatcher.quick.asyncio.create_subprocess_exec", spawn):
            async for _ in quick.stream("ask me", cfg, **kw):
                pass
        return list(spawn.await_args.args)

    async def test_bash_is_explicitly_disallowed_when_not_granted(self):
        # the headless CLI auto-permits a sandboxed read-only Bash otherwise
        cmd = await self._cmd(_cfg(), tools=["Read", "Glob"])
        self.assertIn("--disallowedTools", cmd)
        self.assertEqual(cmd[cmd.index("--disallowedTools") + 1], "Bash")

    async def test_granted_bash_is_not_disallowed(self):
        cmd = await self._cmd(_cfg(), tools=["Bash(ls:*)"])
        self.assertNotIn("--disallowedTools", cmd)

    async def test_model_override_beats_config(self):
        cmd = await self._cmd(_cfg(), model_override="claude-haiku-4-5")
        self.assertEqual(cmd[cmd.index("--model") + 1], "claude-haiku-4-5")

    async def test_resume_and_budget_are_passed(self):
        cmd = await self._cmd(_cfg(quick_max_cost_usd=0.25),
                              resume_session_id="sess-9")
        self.assertEqual(cmd[cmd.index("--resume") + 1], "sess-9")
        self.assertEqual(cmd[cmd.index("--max-budget-usd") + 1], "0.25")

    async def test_area_context_rides_the_system_prompt(self):
        cmd = await self._cmd(_cfg(), context="# Area: tasks")
        system = cmd[cmd.index("--system-prompt") + 1]
        self.assertIn("Jarvis", system)             # the base persona survives
        self.assertIn("# Area: tasks", system)


if __name__ == "__main__":
    unittest.main()
