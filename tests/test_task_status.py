"""One status reader — brief, toggle and the task list must agree.

Three parsers used to answer "is this task open?" three different ways; the
worst case was a status-less file: the dashboard listed it open while the
brief's regex saw no `status: open` line and declared the vault quiet, so the
morning agent never ran for a task staring at the user. The shared reader
defaults missing status to open everywhere — failing toward doing the work.
"""

import unittest

from dispatcher import task_status as ts


CORPUS = {
    "plain open": ("---\nstatus: open\n---\n", True),
    "plain done": ("---\nstatus: done\n---\n", False),
    "capital": ("---\nStatus: Open\n---\n", True),
    "spaced": ("---\nstatus:   open\n---\n", True),
    "no status line": ("---\ntitle: T\n---\n", True),
    "no frontmatter": ("just a body\n", True),
    "pending": ("---\nstatus: pending\n---\n", False),
    "prose only": ("---\ntitle: T\nstatus: done\n---\nstatus: open\n", False),
}


class TestStatusAgreement(unittest.TestCase):
    def test_corpus_agrees(self):
        for name, (text, want_open) in CORPUS.items():
            with self.subTest(name):
                self.assertEqual(ts.is_open_text(text), want_open, name)

    def test_set_status_round_trip(self):
        for name, (text, want_open) in CORPUS.items():
            if text.startswith("---") and text.find("---", 3) == -1:
                continue
            with self.subTest(name):
                flipped = ts.set_status(text, "done" if want_open else "open")
                self.assertEqual(ts.is_open_text(flipped), not want_open, name)
                back = ts.set_status(flipped, "open" if want_open else "done")
                self.assertEqual(ts.is_open_text(back), want_open, name)

    def test_set_status_flips_when_asked_to_flip(self):
        for name, (text, want_open) in CORPUS.items():
            with self.subTest(name):
                target = "done" if want_open else "open"
                out = ts.set_status(text, target)
                self.assertNotEqual(out, text, name)
                self.assertEqual(ts.is_open_text(out), not want_open, name)

    def test_unterminated_raises(self):
        with self.assertRaises(ValueError):
            ts.set_status("---\nstatus: open\n", "done")
