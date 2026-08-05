"""toggle_task — the dashboard checkbox that never worked.

The old implementation did a bare substring replace (`"status: open" in
text`), so: a `Status: Open` file was written back byte-identical while
reporting success; prose mentioning "status: done" in the body could flip the
reported state; and a file with no status line at all got a no-op write plus
a confident answer. The checkbox has been decorative since Phase D.

These tests pin the fixed behaviour: frontmatter parsing via the same
`parse_task_file` the task list uses, case/space tolerance, body prose never
touched, and a missing status field inserted rather than silently no-op'd.
"""

import unittest
from pathlib import Path
import tempfile

from dispatcher import vault as vault_mod

_TMPS = []


def _vault_with(name: str, text: str) -> Path:
    tmp = tempfile.TemporaryDirectory()
    _TMPS.append(tmp)  # keep alive for the test session
    v = Path(tmp.name)
    (v / "tasks").mkdir(parents=True)
    (v / "tasks" / name).write_text(text)
    return v


class TestToggleTask(unittest.TestCase):
    def test_round_trip_flips_and_writes(self):
        v = _vault_with("a.md", "---\ntitle: T\nstatus: open\n---\nbody\n")
        self.assertEqual(vault_mod.toggle_task(v, "a.md")["status"], "done")
        text = (v / "tasks" / "a.md").read_text()
        self.assertIn("status: done", text)
        self.assertIn("body", text)
        self.assertEqual(vault_mod.toggle_task(v, "a.md")["status"], "open")

    def test_case_and_space_tolerant(self):
        v = _vault_with("b.md", "---\ntitle: T\nStatus:   Open\n---\nbody\n")
        out = vault_mod.toggle_task(v, "b.md")
        self.assertEqual(out["status"], "done")
        self.assertIn("status: done", (v / "tasks" / "b.md").read_text())

    def test_body_prose_never_touched(self):
        body = "note: the old status: done line in prose stays\n"
        v = _vault_with("c.md", f"---\ntitle: T\nstatus: open\n---\n{body}")
        vault_mod.toggle_task(v, "c.md")
        text = (v / "tasks" / "c.md").read_text()
        self.assertIn("status: done\n---", text)  # frontmatter flipped
        self.assertIn("the old status: done line in prose stays", text)

    def test_missing_status_is_inserted_not_noop(self):
        before = "---\ntitle: T\n---\nbody\n"
        v = _vault_with("d.md", before)
        out = vault_mod.toggle_task(v, "d.md")
        self.assertEqual(out["status"], "done")  # missing reads as open
        after = (v / "tasks" / "d.md").read_text()
        self.assertNotEqual(after, before)
        self.assertIn("status: done", after)

    def test_no_frontmatter_gets_one(self):
        v = _vault_with("e.md", "just a body\n")
        out = vault_mod.toggle_task(v, "e.md")
        self.assertEqual(out["status"], "done")
        text = (v / "tasks" / "e.md").read_text()
        self.assertTrue(text.startswith("---\nstatus: done\n---\n"))

    def test_unterminated_frontmatter_raises(self):
        v = _vault_with("f.md", "---\ntitle: T\nstatus: open\nbody\n")
        with self.assertRaises(ValueError):
            vault_mod.toggle_task(v, "f.md")

    def test_traversal_rejected(self):
        v = _vault_with("g.md", "---\nstatus: open\n---\n")
        with self.assertRaises(ValueError):
            vault_mod.toggle_task(v, "../memory/USER.md")
