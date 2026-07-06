"""Phase C unit tests: area registry parsing + trigger routing (uses the real
areas/tasks/SKILL.md so the shipped frontmatter stays valid)."""

import unittest
from pathlib import Path

from dispatcher.areas import AreaRegistry

ROOT = Path(__file__).parent.parent


class TestAreaRegistry(unittest.TestCase):
    def setUp(self):
        self.reg = AreaRegistry(ROOT / "areas")

    def test_tasks_area_loads(self):
        areas = self.reg.load()
        self.assertIn("tasks", areas)
        tasks = areas["tasks"]
        self.assertIn("add task", tasks.triggers)
        self.assertIn("morning brief", tasks.quick_triggers)
        self.assertTrue(any(t.startswith("Edit(vault/tasks/") for t in tasks.allowed_tools))
        self.assertIn("Read", tasks.quick_allowed_tools)

    def test_context_includes_skill_and_claude_md(self):
        ctx = self.reg.get("tasks").context()
        self.assertIn("Task file format", ctx)
        self.assertIn("vault/briefs/YYYY-MM-DD.md", ctx)  # from area CLAUDE.md

    def test_trigger_routes_agentic(self):
        area, hint = self.reg.match("add a task: renew the domain")
        self.assertEqual(area.name, "tasks")
        self.assertEqual(hint, "agentic")

    def test_spoken_phrasing_matches_as_subsequence(self):
        for text in [
            "mark the domain task as done",
            "complete that renewal task",
            "can you add a new task for me",
            "remind me to water the plants",
        ]:
            area, hint = self.reg.match(text)
            self.assertIsNotNone(area, text)
            self.assertEqual(hint, "agentic", text)

    def test_quick_trigger_wins(self):
        area, hint = self.reg.match("hey, read my morning brief please")
        self.assertEqual(area.name, "tasks")
        self.assertEqual(hint, "quick")

    def test_no_match(self):
        area, hint = self.reg.match("what is the capital of France?")
        self.assertIsNone(area)
        self.assertIsNone(hint)

    def test_missing_dir_is_empty(self):
        reg = AreaRegistry(ROOT / "no-such-dir")
        self.assertEqual(reg.load(), {})
        self.assertEqual(reg.match("add a task: x"), (None, None))


if __name__ == "__main__":
    unittest.main()
