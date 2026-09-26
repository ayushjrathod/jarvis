"""Nightly agents were told today is yesterday.

`build_consolidation` stamped `{{DATE}}` with `now()[:10]` — UTC — while the
timer fires at 02:30 local (21:00 UTC the previous day at +5:30). Every date
in USER.md/MEMORY.md and every kg valid_at landed a day early, and the brief
(which uses local time) disagreed with the memory blocks. `local_today()` is
the same wall clock systemd and run_agent.py use.
"""

import unittest
from datetime import date, datetime, timezone
from unittest import mock

from dispatcher import db as db_mod


class FakeDateTime(datetime):
    """Pretend the box is at +5:30 with wall clock 2026-08-06 02:30."""
    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls(2026, 8, 6, 2, 30, 0)
        return datetime(2026, 8, 5, 21, 0, 0, tzinfo=timezone.utc)


class TestLocalToday(unittest.TestCase):
    def test_matches_local_wall_clock(self):
        self.assertEqual(db_mod.local_today(), date.today().isoformat())

    def test_yesterday_bug_boundary(self):
        with mock.patch.object(db_mod, "datetime", FakeDateTime):
            # UTC still says the 5th while the box is already into the 6th.
            self.assertEqual(db_mod.now()[:10], "2026-08-05")
            self.assertEqual(db_mod.local_today(), "2026-08-06")

    def test_consolidation_no_longer_uses_utc_date(self):
        import inspect
        from dispatcher import memory as mem_mod
        src = inspect.getsource(mem_mod.build_consolidation)
        self.assertNotIn("now()[:10]", src)
        self.assertIn("local_today()", src)
