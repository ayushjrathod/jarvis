"""Smoke-level unit tests — stdlib unittest, no network, no claude invocation.

Run: .venv/bin/python -m unittest discover tests
"""

import tempfile
import unittest
from pathlib import Path

from dispatcher.classifier import classify
from dispatcher.db import Database
from dispatcher.queue_watcher import parse_task_file
from dispatcher.runner import is_refusal
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


if __name__ == "__main__":
    unittest.main()
