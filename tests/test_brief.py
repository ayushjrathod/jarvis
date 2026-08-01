"""Deterministic daily-brief pre-check (2026-08-02).

The brief wrote "clean slate" every day from 07-24 to 08-01 at ~$0.40 a run,
because the vault holds one done task and two notes from early July. These pin
the quiet check: it must be quiet only when there is genuinely nothing, and it
must never swallow a day that has material.
"""

import tempfile
import time
import unittest
from datetime import date
from pathlib import Path

from dispatcher import brief


class TestQuietVault(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for d in ("tasks", "notes", "briefs"):
            (self.root / "vault" / d).mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _task(self, name, status):
        (self.root / "vault" / "tasks" / name).write_text(
            f"---\ntitle: t\nstatus: {status}\n---\nbody\n")

    def _note(self, name, age_days=0):
        p = self.root / "vault" / "notes" / name
        p.write_text("note\n")
        if age_days:
            old = time.time() - age_days * 86400
            import os
            os.utime(p, (old, old))
        return p

    def test_empty_vault_is_quiet(self):
        self.assertTrue(brief.is_quiet(self.root))

    def test_a_done_task_and_stale_notes_are_still_quiet(self):
        """The live case: one done task from 07-06, two notes from 07-05."""
        self._task("renew.md", "done")
        self._note("old-a.md", age_days=27)
        self._note("old-b.md", age_days=28)
        self.assertTrue(brief.is_quiet(self.root))

    def test_an_open_task_is_not_quiet(self):
        self._task("renew.md", "open")
        self.assertFalse(brief.is_quiet(self.root))

    def test_a_fresh_note_is_not_quiet(self):
        self._note("today.md")
        self.assertFalse(brief.is_quiet(self.root))

    def test_note_just_outside_the_window_is_quiet(self):
        self._note("older.md", age_days=4)
        self.assertTrue(brief.is_quiet(self.root))
        self.assertFalse(brief.is_quiet(self.root, days=5))

    def test_status_open_matches_only_the_frontmatter_field(self):
        """Prose mentioning the word must not read as an open task."""
        (self.root / "vault" / "tasks" / "x.md").write_text(
            "---\nstatus: done\n---\nI left the status open for a while.\n")
        self.assertTrue(brief.is_quiet(self.root))

    def test_unreadable_task_defers_to_the_agent(self):
        """Fail toward doing the real work, never toward skipping it."""
        p = self.root / "vault" / "tasks" / "bad.md"
        p.mkdir()          # a directory named *.md: read_text raises
        self.assertFalse(brief.is_quiet(self.root))


class TestQuietBrief(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_render_has_the_agent_s_sections(self):
        out = brief.render_quiet(date(2026, 8, 2))
        self.assertIn("# Brief — 2026-08-02", out)
        for h in ("## Today", "## Open tasks", "## Recent notes"):
            self.assertIn(h, out)
        self.assertNotIn("## Email", out)   # removed 2026-08-01, stays removed

    def test_write_quiet_creates_the_dated_file(self):
        p = brief.write_quiet(self.root, date(2026, 8, 2))
        self.assertEqual(p.name, "2026-08-02.md")
        self.assertTrue(p.exists())
        self.assertIn("No open tasks.", p.read_text())


if __name__ == "__main__":
    unittest.main()
