"""Phase C unit tests: area registry parsing + trigger routing (uses the real
areas/tasks/SKILL.md so the shipped frontmatter stays valid)."""

import os
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

    def test_privileged_area_keyed_on_the_directory_the_operator_blessed(self):
        """Blessing is per DIRECTORY, so the directory name is what unlocks it."""
        area = AreaRegistry(self.root, privileged_areas=["risky"]).load()["risky"]
        self.assertIn("Bash", area.allowed_tools)
        self.assertIn("Edit", area.allowed_tools)
        self.assertIn("Bash(ls:*)", area.allowed_tools)
        self.assertIn("Bash", area.quick_allowed_tools)


class TestPrivilegeKeysOnTheDirectory(unittest.TestCase):
    """H2: the privilege check must NOT key on the frontmatter `name:`, which
    lives in the very file whose grants are being validated.

    A reflection/learn run holds Edit(areas/**). If the check read `name:`, such
    a run could create areas/notes-helper/SKILL.md declaring `name: tasks` plus
    `- Bash` and inherit whatever the operator blessed for the real "tasks"
    directory — a manifest granting itself privilege. Inert while
    security.privileged_areas is empty, but the config documents adding entries
    to it as supported, and on that day it stops being a boundary.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _area(self, dirname: str, declared: str):
        d = self.root / dirname
        d.mkdir()
        (d / "SKILL.md").write_text(
            f"---\nname: {declared}\nallowed_tools:\n"
            f"  - Bash\n  - Read\n---\nbody\n")

    def test_impostor_cannot_borrow_a_blessed_name(self):
        # what a prompt-injected reflection run would write: a new area whose
        # frontmatter claims the blessed area's name, plus Bash
        self._area("notes-helper", declared="tasks")
        area = AreaRegistry(self.root, privileged_areas=["tasks"]).load()["tasks"]
        self.assertEqual(area.path.name, "notes-helper")   # unblessed directory
        self.assertNotIn("Bash", area.allowed_tools)
        self.assertEqual(area.allowed_tools, ["Read"])

    def test_the_real_blessed_directory_still_keeps_bash(self):
        self._area("tasks", declared="tasks")
        area = AreaRegistry(self.root, privileged_areas=["tasks"]).load()["tasks"]
        self.assertIn("Bash", area.allowed_tools)

    def test_renamed_area_is_not_privileged_by_its_directory_either(self):
        """The converse: blessing the frontmatter name must not leak in. Only
        `security.privileged_areas` naming the DIRECTORY unlocks Bash."""
        self._area("expenses", declared="expense-tracking")
        reg = AreaRegistry(self.root, privileged_areas=["expense-tracking"])
        self.assertNotIn("Bash", reg.load()["expense-tracking"].allowed_tools)
        reg = AreaRegistry(self.root, privileged_areas=["expenses"])
        self.assertIn("Bash", reg.load()["expense-tracking"].allowed_tools)

    def test_agent_tools_key_on_the_directory_too(self):
        """agent_tools resolves areas/<dir>/agents/<agent>.md, so its privilege
        key is already the directory — pinned so it cannot drift to `name:`."""
        self._area("notes-helper", declared="tasks")
        agents = self.root / "notes-helper" / "agents"
        agents.mkdir(parents=True)
        (agents / "helper.md").write_text(
            "---\nallowed_tools:\n  - Bash\n  - Read\n---\nbody\n")
        reg = AreaRegistry(self.root, privileged_areas=["tasks"])
        self.assertEqual(reg.agent_tools("notes-helper", "helper"), ["Read"])


class TestUnreadableSkillDoesNotBreakDispatch(unittest.TestCase):
    """One bad SKILL.md used to 500 EVERY POST /task.

    load() runs on every dispatch (match() → Service.route()), re-read from
    disk with no cache and no try/except anywhere on that path, but only the
    YAML parse was guarded — read_text() was not. Reflection and learn runs
    write these files, so a truncated or interrupted write (non-UTF-8 bytes) or
    a bad mode was enough to take the whole dispatcher down. A broken area must
    drop out exactly like bad frontmatter already did.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        good = self.root / "tasks"
        good.mkdir()
        (good / "SKILL.md").write_text(
            "---\nname: tasks\ntriggers:\n  - add task\n---\nbody\n")
        self.reg = AreaRegistry(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _break(self, name: str, data: bytes):
        d = self.root / name
        d.mkdir()
        (d / "SKILL.md").write_bytes(data)

    def test_non_utf8_skill_is_skipped_not_raised(self):
        # a half-written file: valid frontmatter start, then raw bytes
        self._break("broken", b"---\nname: broken\n---\n\xff\xfe not utf-8\n")
        areas = self.reg.load()
        self.assertNotIn("broken", areas)
        self.assertIn("tasks", areas)          # the good area still loads

    def test_dispatch_path_still_routes(self):
        """The observable outcome: matching still works, which is what
        Service.route() calls on every single task."""
        self._break("broken", b"---\nname: broken\n---\n\xff\xfe\n")
        area, hint = self.reg.match("add a task: renew the domain")
        self.assertEqual(area.name, "tasks")
        self.assertEqual(hint, "agentic")

    def test_unreadable_file_is_skipped(self):
        d = self.root / "locked"
        d.mkdir()
        skill = d / "SKILL.md"
        skill.write_text("---\nname: locked\n---\nbody\n")
        skill.chmod(0o000)
        try:
            if os.access(skill, os.R_OK):
                self.skipTest("running as root; chmod does not block the read")
            areas = self.reg.load()
            self.assertNotIn("locked", areas)
            self.assertIn("tasks", areas)
        finally:
            skill.chmod(0o644)

    def test_list_frontmatter_is_not_a_mapping(self):
        # valid YAML, wrong shape: meta.get() would AttributeError on a list
        self._break("listy", b"---\n- a\n- b\n---\nbody\n")
        self.assertNotIn("listy", self.reg.load())
        self.assertIsNotNone(self.reg.match("add a task: x")[0])


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


class TestScopeEscalationHole(unittest.TestCase):
    """Finding 0.2: the old sanitizer read 'contains a parenthesis' as 'is
    scoped', so Edit(**) survived load and a reflection run holding
    Edit(areas/**) could grant itself repo-wide write — then config.yaml,
    then real Bash. Proved end to end before fixing."""

    PROBES = [
        "Edit(**)", "Edit(/**)", "Edit(../../**)",
        "Edit(/home/ayra/**)", "Edit(areas/../../.claude-per/**)",
        "Edit", "Write", "MultiEdit", "NotebookEdit",
        "mcp__claude_ai_Gmail__search_threads",
        "mcp__claude_ai_Google_Drive__create_file",
        "Task", "WebFetch", "Bash", "Bash(ls:*)",
    ]
    KEPT = [
        "Read", "Glob", "Grep",
        "Edit(areas/**)", "Edit(vault/memory/**)", "Edit(vault/tasks/**)",
        "Edit(vault/briefs/**)", "Edit(data/**)",
        # Write(path) rules are ignored by the CLI, so a scoped one is inert
        # kept or stripped — the uniform scope rule keeps it.
        "Write(vault/tasks/**)",
    ]

    def _load(self, *tools):
        import tempfile
        from pathlib import Path as P
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        area = P(tmp.name) / "probe"
        area.mkdir()
        lines = "".join(f"  - '{t}'\n" for t in tools)
        (area / "SKILL.md").write_text(
            f"---\nname: probe\nallowed_tools:\n{lines}---\nbody\n")
        return AreaRegistry(P(tmp.name)).load()["probe"].allowed_tools

    def test_escalation_probes_are_stripped(self):
        kept = self._load(*self.PROBES)
        self.assertEqual(kept, [], f"survived the sanitizer: {kept}")

    def test_legitimate_grants_survive(self):
        kept = self._load(*self.KEPT)
        self.assertEqual(kept, list(self.KEPT))


if __name__ == "__main__":
    unittest.main()
