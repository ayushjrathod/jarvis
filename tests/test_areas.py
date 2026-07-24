"""Phase C unit tests: area registry parsing + trigger routing (uses the real
areas/tasks/SKILL.md so the shipped frontmatter stays valid)."""

import tempfile
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


class TestMalformedTriggers(unittest.TestCase):
    """Hand-edited frontmatter must not be able to 500 every /task: empty and
    non-string trigger entries are dropped at parse time (an empty trigger
    used to IndexError inside _subsequence on every match call)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        area = Path(self.tmp.name) / "broken"
        area.mkdir()
        (area / "SKILL.md").write_text(
            "---\nname: broken\ntriggers:\n  - ''\n  - '   '\n  - 42\n"
            "  - real trigger\nquick_triggers:\n  - ''\n---\nbody\n"
        )
        self.reg = AreaRegistry(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_and_nonstring_triggers_dropped(self):
        area = self.reg.load()["broken"]
        self.assertEqual(area.triggers, ["real trigger"])
        self.assertEqual(area.quick_triggers, [])

    def test_match_does_not_crash(self):
        area, hint = self.reg.match("this mentions a real trigger phrase")
        self.assertEqual(area.name, "broken")
        self.assertEqual(hint, "agentic")
        self.assertEqual(self.reg.match("unrelated text"), (None, None))


class TestFrontmatterToolValidation(unittest.TestCase):
    """H2: SKILL.md frontmatter is the privilege manifest, so a reflection/learn
    run with Edit(areas/**) could be prompt-injected into writing Bash into it.
    Load-time validation strips Bash and unscoped Edit/Write from any area not
    listed in security.privileged_areas, making such a grant inert."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        area = self.root / "risky"
        area.mkdir()
        (area / "SKILL.md").write_text(
            "---\n"
            "name: risky\n"
            "allowed_tools:\n"
            "  - Bash\n"
            "  - Read\n"
            "  - Edit\n"                    # unscoped -> stripped
            "  - 'Edit(areas/**)'\n"        # scoped -> kept
            "  - 'Bash(ls:*)'\n"           # scoped Bash still stripped
            "quick_allowed_tools:\n"
            "  - Bash\n"
            "  - Read\n"
            "---\nbody\n"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_non_privileged_area_strips_bash_and_unscoped_writes(self):
        area = AreaRegistry(self.root).load()["risky"]
        self.assertEqual(area.allowed_tools, ["Read", "Edit(areas/**)"])
        # quick tools are validated the same way
        self.assertEqual(area.quick_allowed_tools, ["Read"])

    def test_privileged_area_keeps_bash(self):
        area = AreaRegistry(self.root, privileged_areas=["risky"]).load()["risky"]
        self.assertIn("Bash", area.allowed_tools)
        self.assertIn("Edit", area.allowed_tools)
        self.assertIn("Bash(ls:*)", area.allowed_tools)
        self.assertIn("Bash", area.quick_allowed_tools)


class TestAgentToolResolution(unittest.TestCase):
    """Timer agents declare grants the area itself doesn't carry. Those used to
    ride in on client metadata, which the H1 trust boundary strips — silently
    costing the daily brief its write access. The dispatcher resolves the agent
    file from disk instead, so a caller may NAME an agent but never grant it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        agents = self.root / "tasks" / "agents"
        agents.mkdir(parents=True)
        (self.root / "tasks" / "SKILL.md").write_text(
            "---\nname: tasks\nallowed_tools:\n  - Read\n---\nbody\n")
        (agents / "daily-brief.md").write_text(
            "---\ntask_type: daily-brief\nallowed_tools:\n"
            "  - Read\n  - 'Edit(vault/briefs/**)'\n  - Bash\n---\nwrite it\n")
        (agents / "no-tools.md").write_text("---\ntask_type: x\n---\nbody\n")
        (agents / "bodyonly.md").write_text("no frontmatter here\n")
        self.reg = AreaRegistry(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_resolves_declared_tools_from_disk(self):
        self.assertEqual(self.reg.agent_tools("tasks", "daily-brief"),
                         ["Read", "Edit(vault/briefs/**)"])  # Bash stripped

    def test_privileged_area_keeps_bash_in_agent_file(self):
        reg = AreaRegistry(self.root, privileged_areas=["tasks"])
        self.assertIn("Bash", reg.agent_tools("tasks", "daily-brief"))

    def test_missing_or_toolless_agents_return_none(self):
        for area, agent in [("tasks", "nope"), ("nope", "daily-brief"),
                            ("tasks", "no-tools"), ("tasks", "bodyonly")]:
            with self.subTest(agent=f"{area}/{agent}"):
                self.assertIsNone(self.reg.agent_tools(area, agent))

    def test_traversal_attempts_are_refused(self):
        for area, agent in [("tasks", "../../SKILL"), ("..", "daily-brief"),
                            ("tasks", "/etc/passwd"), ("tasks", ""), ("", "x")]:
            with self.subTest(agent=f"{area}/{agent}"):
                self.assertIsNone(self.reg.agent_tools(area, agent))

    def test_real_daily_brief_agent_grants_briefs_write(self):
        """The live regression: this grant must survive to the runner."""
        tools = AreaRegistry(ROOT / "areas").agent_tools("tasks", "daily-brief")
        self.assertIn("Edit(vault/briefs/**)", tools)


if __name__ == "__main__":
    unittest.main()
