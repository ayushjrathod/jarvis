"""Inbox arrival watcher — the first event-driven (not clock-driven) trigger.

`InboxWatcher.step` is the whole policy and is a pure function over successive
scans, so none of this needs a filesystem, a clock, or a dispatcher.
"""

import tempfile
import unittest
from pathlib import Path

from dispatcher.inbox import InboxWatcher, scan


class TestStep(unittest.TestCase):
    def setUp(self):
        self.w = InboxWatcher()

    def test_first_pass_seeds_without_firing(self):
        # a restart must not re-announce everything already sitting there
        self.assertEqual(self.w.step({"old.pdf": (1.0, 10)}), ([], []))
        self.assertEqual(self.w.step({"old.pdf": (1.0, 10)}), ([], []))

    def test_new_file_fires_only_after_it_settles(self):
        self.w.step({})
        self.assertEqual(self.w.step({"a.pdf": (1.0, 100)})[0], [])     # seen once
        self.assertEqual(self.w.step({"a.pdf": (1.0, 100)})[0], ["a.pdf"])
        self.assertEqual(self.w.step({"a.pdf": (1.0, 100)})[0], [])     # not again

    def test_file_still_being_written_does_not_fire(self):
        self.w.step({})
        for size in (100, 5000, 20000):          # a big PDF landing in chunks
            self.assertEqual(self.w.step({"big.pdf": (1.0, size)})[0], [])
        self.assertEqual(self.w.step({"big.pdf": (1.0, 20000)})[0], ["big.pdf"])

    def test_modified_file_fires_again(self):
        self.w.step({"a.md": (1.0, 10)})
        self.assertEqual(self.w.step({"a.md": (2.0, 20)})[0], [])
        self.assertEqual(self.w.step({"a.md": (2.0, 20)})[0], ["a.md"])

    def test_several_files_fire_together_sorted(self):
        self.w.step({})
        self.w.step({"b.md": (1.0, 1), "a.md": (1.0, 1)})
        self.assertEqual(self.w.step({"b.md": (1.0, 1), "a.md": (1.0, 1)})[0],
                         ["a.md", "b.md"])

    def test_file_removed_mid_settle_is_forgotten(self):
        self.w.step({})
        self.w.step({"gone.md": (1.0, 1)})
        self.assertEqual(self.w.step({}), ([], []))
        self.assertEqual(self.w.pending, {})

    def test_removals_are_reported_separately_from_arrivals(self):
        # a deletion needs a reindex (else the file stays searchable after it's
        # gone) but must not produce an "indexed a new file" notification
        self.w.step({"a.md": (1.0, 1), "b.md": (1.0, 1)})
        arrived, removed = self.w.step({"a.md": (1.0, 1)})
        self.assertEqual((arrived, removed), ([], ["b.md"]))

    def test_deleted_file_returning_fires_again(self):
        self.w.step({})
        self.w.step({"a.md": (1.0, 1)})
        self.w.step({"a.md": (1.0, 1)})           # fired
        self.assertEqual(self.w.step({}), ([], ["a.md"]))  # deleted -> reindex
        self.w.step({"a.md": (1.0, 1)})
        self.assertEqual(self.w.step({"a.md": (1.0, 1)})[0], ["a.md"])


class TestScan(unittest.TestCase):
    def test_skips_readme_dotfiles_and_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "README.md").write_text("the convention doc")
            (root / ".hidden").write_text("x")
            (root / "sub").mkdir()
            (root / "sub" / "note.md").write_text("real content")
            (root / "top.txt").write_text("also real")
            names = set(scan(root))
        self.assertEqual(names, {"top.txt", "sub/note.md"})

    def test_missing_directory_is_empty_not_an_error(self):
        self.assertEqual(scan(Path("/nonexistent/inbox")), {})

    def test_reports_mtime_and_size(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.txt"
            p.write_text("hello")
            (mtime, size) = scan(Path(d))["a.txt"]
        self.assertEqual(size, 5)
        self.assertGreater(mtime, 0)


if __name__ == "__main__":
    unittest.main()
