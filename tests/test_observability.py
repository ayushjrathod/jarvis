"""Phase H observability tests: latency stats, step parsing, run_steps
persistence, migrations, /stats aggregates. stdlib unittest, no network.
"""

import tempfile
import unittest
from pathlib import Path

from dispatcher.runner import _steps_from_event, build_cmd
from dispatcher.config import Config
from dispatcher.db import Database
from dispatcher.telemetry import stream_stats


class TestStreamStats(unittest.TestCase):
    def test_no_deltas_is_empty(self):
        self.assertEqual(stream_stats(0.0, []), {})

    def test_single_delta(self):
        s = stream_stats(1.0, [1.5])
        self.assertEqual(s["ttft_ms"], 500.0)
        self.assertIsNone(s["itl_p95_ms"])

    def test_many_deltas(self):
        times = [1.0 + 0.1 * i for i in range(1, 11)]  # 100ms apart
        s = stream_stats(1.0, times, output_tokens=200)
        self.assertEqual(s["ttft_ms"], 100.0)
        self.assertAlmostEqual(s["itl_p95_ms"], 100.0, delta=1)
        self.assertAlmostEqual(s["tokens_per_s"], 200 / 1.0, delta=1)

    def test_delta_count_fallback_without_tokens(self):
        s = stream_stats(0.0, [0.5, 1.0])
        self.assertAlmostEqual(s["tokens_per_s"], 2.0, delta=0.1)


class TestStepParsing(unittest.TestCase):
    def test_init(self):
        steps = _steps_from_event(
            {"type": "system", "subtype": "init", "model": "claude-sonnet-5"})
        self.assertEqual(steps, [{"type": "init", "summary": "claude-sonnet-5"}])

    def test_assistant_text_and_tool_use(self):
        steps = _steps_from_event({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "thinking about it"},
            {"type": "tool_use", "name": "Read",
             "input": {"file_path": "vault/tasks/x.md"}},
        ]}})
        self.assertEqual([s["type"] for s in steps], ["text", "tool_use"])
        self.assertIn("Read", steps[1]["summary"])
        self.assertIn("vault/tasks/x.md", steps[1]["summary"])

    def test_tool_result_list_content_and_error_flag(self):
        steps = _steps_from_event({"type": "user", "message": {"content": [
            {"type": "tool_result", "is_error": True,
             "content": [{"type": "text", "text": "no such file"}]},
        ]}})
        self.assertEqual(steps[0]["type"], "tool_result")
        self.assertTrue(steps[0]["summary"].startswith("ERROR: no such file"))

    def test_permission_denial_is_flagged(self):
        """2026-07-21..23: the CLI reports a missing allowedTools grant as an
        ordinary is_error tool_result, so three daily briefs ended
        subtype=success having written nothing."""
        denial = ("Claude requested permissions to write to "
                  + "/home/ayra/Documents/code/jarvis/vault/briefs/x.md" * 4
                  + ", but you haven't granted it yet.")
        steps = _steps_from_event({"type": "user", "message": {"content": [
            {"type": "tool_result", "is_error": True, "content": denial},
        ]}})
        # flagged off the full text, though the clip hides the tell-tale tail
        self.assertTrue(steps[0]["denied"])
        self.assertNotIn("haven't granted", steps[0]["summary"])

    def test_ordinary_errors_are_not_denials(self):
        steps = _steps_from_event({"type": "user", "message": {"content": [
            {"type": "tool_result", "is_error": True, "content": "no such file"},
            {"type": "tool_result", "content": "requested permissions"},
        ]}})
        self.assertNotIn("denied", steps[0])   # an error, but not a denial
        self.assertNotIn("denied", steps[1])   # the phrase, but not an error

    def test_summaries_clipped(self):
        steps = _steps_from_event({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "x" * 1000}]}})
        self.assertLessEqual(len(steps[0]["summary"]), 200)

    def test_result_and_unknown(self):
        self.assertEqual(
            _steps_from_event({"type": "result", "subtype": "success"}),
            [{"type": "result", "summary": "success"}])
        self.assertEqual(_steps_from_event({"type": "stream_event"}), [])

    def test_build_cmd_streams_json(self):
        cfg = Config(root=Path("."))
        cfg.claude_bin = "claude"
        cfg.budgets = {}
        cmd = build_cmd("t", cfg, None, [])
        self.assertIn("stream-json", cmd)
        self.assertIn("--verbose", cmd)

    def test_build_cmd_disallows_ungranted_bash(self):
        """Layer-13 probe (2026-07-19): headless CLI auto-permits sandboxed
        read-only Bash unless explicitly disallowed."""
        cfg = Config(root=Path("."))
        cfg.claude_bin = "claude"
        cfg.budgets = {}
        cmd = build_cmd("t", cfg, None, ["Read", "Glob", "Grep"])
        self.assertIn("--disallowedTools", cmd)
        self.assertEqual(cmd[cmd.index("--disallowedTools") + 1], "Bash")
        for granted in (["Bash"], ["Read", "Bash(git status)"]):
            self.assertNotIn("--disallowedTools",
                             build_cmd("t", cfg, None, granted))


class TestRunStepsAndStats(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.db"
        self.db = Database(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_migrations_idempotent(self):
        Database(self.path)  # re-open runs the ALTERs again — must not raise
        with self.db._conn() as c:
            cols = {r["name"] for r in c.execute("PRAGMA table_info(runs)")}
        self.assertIn("ttft_ms", cols)
        self.assertIn("tokens_per_s", cols)

    def test_run_steps_roundtrip(self):
        t = self.db.create_task("do", "api", "agentic")
        rid = self.db.create_run(t["id"], 1, "m")
        self.db.add_run_steps(rid, [
            {"type": "init", "summary": "model", "elapsed_ms": 10},
            {"type": "tool_use", "summary": "Read x", "elapsed_ms": 900},
        ])
        steps = self.db.get_run_steps(rid)
        self.assertEqual([s["step_type"] for s in steps], ["init", "tool_use"])
        self.assertEqual(steps[1]["elapsed_ms"], 900)
        self.db.add_run_steps(rid, [])  # no-op, no raise

    def test_finish_run_persists_latency(self):
        t = self.db.create_task("q", "voice", "quick")
        rid = self.db.create_run(t["id"], 1, "m")
        self.db.finish_run(rid, "done", ttft_ms=850.5, tokens_per_s=12.3)
        run = self.db.get_task(t["id"])["runs"][0]
        self.assertAlmostEqual(run["ttft_ms"], 850.5)

    def test_stats_summary(self):
        for status, kind, source in [("done", "quick", "voice"),
                                     ("done", "agentic", "timer"),
                                     ("failed", "quick", "voice")]:
            t = self.db.create_task("x", source, kind)
            rid = self.db.create_run(t["id"], 1, "m")
            self.db.finish_run(rid, status, cost_usd=0.1,
                               ttft_ms=1000 if kind == "quick" else None)
            self.db.set_task_status(t["id"], status)
        rt = self.db.create_task("reflect", "reflection", "agentic")
        rrid = self.db.create_run(rt["id"], 1, "m")
        self.db.finish_run(rrid, "done", output_text="Patched tasks skill.")
        self.db.set_task_status(rt["id"], "done")

        s = self.db.stats_summary(days=7)
        self.assertEqual(s["tasks_by_status"]["done"], 3)
        self.assertEqual(s["tasks_by_status"]["failed"], 1)
        self.assertAlmostEqual(s["success_rate"], 0.75)
        self.assertAlmostEqual(s["total_cost_usd"], 0.3)
        self.assertEqual(s["quick_latency"]["avg_ttft_ms"], 1000.0)
        self.assertEqual(s["recent_reflections"][0]["output_text"],
                         "Patched tasks skill.")

    def test_by_source_counts_tasks_not_runs(self):
        """A refusal-fallback gives one task two runs; the LEFT JOIN to runs
        then fanned that task out and `n` silently meant "runs", disagreeing
        with tasks_by_status on the very same response."""
        t1 = self.db.create_task("refused then retried", "voice", "quick")
        for attempt, model in ((1, "sonnet"), (2, "opus")):
            rid = self.db.create_run(t1["id"], attempt, model)
            self.db.finish_run(rid, "done", cost_usd=0.10)
        self.db.set_task_status(t1["id"], "done")
        t2 = self.db.create_task("one shot", "voice", "quick")
        self.db.finish_run(self.db.create_run(t2["id"], 1, "sonnet"),
                           "done", cost_usd=0.05)
        self.db.set_task_status(t2["id"], "done")

        by_source = {r["source"]: r for r in self.db.stats_summary(7)["by_source"]}
        self.assertEqual(by_source["voice"]["n"], 2)          # tasks, not 3 runs
        self.assertAlmostEqual(by_source["voice"]["cost_usd"], 0.25)  # all runs


if __name__ == "__main__":
    unittest.main()
