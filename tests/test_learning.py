"""Phase G learning-loop tests: skill telemetry, reflection policy, runner
resume/budget flags, deterministic curator. stdlib unittest, no network.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dispatcher import curator, reflection
from dispatcher.config import Config
from dispatcher.db import Database
from dispatcher.runner import build_cmd


def iso_days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
        timespec="seconds")


def make_cfg(root: Path, **learning) -> Config:
    cfg = Config(root=root)
    cfg.claude_bin = "claude"
    cfg.learning = learning
    return cfg


class TestSkillUsage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_use_and_patch_counters(self):
        self.db.record_skill_use("tasks")
        self.db.record_skill_use("tasks")
        self.db.record_skill_patch("tasks")
        rows = {r["name"]: r for r in self.db.skill_usage_all()}
        self.assertEqual(rows["tasks"]["use_count"], 2)
        self.assertEqual(rows["tasks"]["patch_count"], 1)
        self.assertIsNotNone(rows["tasks"]["last_used_at"])
        self.assertEqual(rows["tasks"]["state"], "active")

    def test_use_reactivates_stale(self):
        self.db.record_skill_use("x")
        self.db.set_skill_state("x", "stale")
        self.db.record_skill_use("x")
        self.assertEqual(self.db.skill_usage_all()[0]["state"], "active")


class TestReflectionPolicy(unittest.TestCase):
    def _result(self, **kw):
        return {"status": "done", "session_id": "s1", "num_turns": 15, **kw}

    def _task(self, source="timer"):
        return {"id": "t", "source": source}

    def test_fires_on_complex_done_run(self):
        self.assertTrue(reflection.should_reflect(
            {}, self._task(), {}, self._result()))

    def test_below_threshold_or_no_session_skips(self):
        self.assertFalse(reflection.should_reflect(
            {}, self._task(), {}, self._result(num_turns=5)))
        self.assertFalse(reflection.should_reflect(
            {}, self._task(), {}, self._result(session_id=None)))
        self.assertFalse(reflection.should_reflect(
            {}, self._task(), {}, self._result(status="failed")))

    def test_meta_work_never_reflects(self):
        for tt in ("reflection", "memory-consolidate", "learn"):
            self.assertFalse(reflection.should_reflect(
                {}, self._task(), {"task_type": tt}, self._result()), tt)
        self.assertFalse(reflection.should_reflect(
            {}, self._task(source="reflection"), {}, self._result()))

    def test_config_gates(self):
        self.assertFalse(reflection.should_reflect(
            {"reflection": False}, self._task(), {}, self._result()))
        self.assertTrue(reflection.should_reflect(
            {"reflection_min_turns": 20}, self._task(), {},
            self._result(num_turns=25)))
        self.assertFalse(reflection.should_reflect(
            {"reflection_min_turns": 20}, self._task(), {},
            self._result(num_turns=15)))


class TestRunnerCmd(unittest.TestCase):
    def test_resume_and_budget_flags(self):
        cfg = make_cfg(Path("."))
        cfg.budgets = {"max_cost_per_task_usd": 3.0}
        cmd = build_cmd("do it", cfg, None, ["Read"],
                        resume_session_id="abc123", max_cost_usd=1.0)
        self.assertIn("--resume", cmd)
        self.assertEqual(cmd[cmd.index("--resume") + 1], "abc123")
        self.assertEqual(cmd[cmd.index("--max-budget-usd") + 1], "1.0")

    def test_defaults_without_resume(self):
        cfg = make_cfg(Path("."))
        cfg.budgets = {"max_cost_per_task_usd": 3.0}
        cmd = build_cmd("do it", cfg, "claude-sonnet-5", [])
        self.assertNotIn("--resume", cmd)
        self.assertEqual(cmd[cmd.index("--max-budget-usd") + 1], "3.0")


class TestRouteMatchArea(unittest.TestCase):
    def test_meta_task_skips_area_matching(self):
        """Reflection prompts must not match area triggers on their own text
        (live bug 2026-07-18: the reflection run matched the tasks area)."""
        from dispatcher.service import Service
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "areas" / "tasks").mkdir(parents=True)
            (root / "areas" / "tasks" / "SKILL.md").write_text(
                "---\nname: tasks\ntriggers:\n  - task done\n---\nbody\n")
            cfg = Config(root=root)
            cfg.db_path = root / "t.db"
            svc = Service(cfg)
            self.assertEqual(svc.route("the task is done", "agentic"),
                             ("agentic", "tasks"))
            self.assertEqual(
                svc.route("the task is done", "agentic", match_area=False),
                ("agentic", None))


class TestCurator(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / "t.db")
        self.cfg = make_cfg(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _area(self, name):
        d = self.root / "areas" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody\n")
        return d

    def _seed(self, name, state, last_used_days=None, first_seen_days=200,
              pinned=0):
        with self.db._conn() as c:
            c.execute(
                "INSERT INTO skill_usage (name, use_count, last_used_at,"
                " first_seen_at, state, pinned) VALUES (?,?,?,?,?,?)",
                (name, 1 if last_used_days is not None else 0,
                 iso_days_ago(last_used_days) if last_used_days is not None else None,
                 iso_days_ago(first_seen_days), state, pinned))

    def test_active_idle_goes_stale(self):
        self._area("expenses")
        self._seed("expenses", "active", last_used_days=45)
        report = curator.run(self.db, self.cfg)
        self.assertEqual(report["stale"], ["expenses"])
        self.assertEqual(self.db.skill_usage_all()[0]["state"], "stale")

    def test_recent_activity_stays_active(self):
        self._area("expenses")
        self._seed("expenses", "active", last_used_days=5)
        report = curator.run(self.db, self.cfg)
        self.assertEqual(report["stale"], [])

    def test_long_stale_archived_and_moved(self):
        self._area("expenses")
        self._seed("expenses", "stale", last_used_days=150)
        report = curator.run(self.db, self.cfg)
        self.assertEqual(report["archived"], ["expenses"])
        self.assertFalse((self.root / "areas" / "expenses").exists())
        self.assertTrue(
            (self.root / "areas" / ".archive" / "expenses" / "SKILL.md").exists())

    def test_protected_and_pinned_exempt(self):
        self._area("tasks")           # in TIMER_AREAS
        self._seed("tasks", "active", last_used_days=400)
        self._area("darling")
        self._seed("darling", "active", last_used_days=400, pinned=1)
        report = curator.run(self.db, self.cfg)
        self.assertEqual(report["stale"], [])
        self.assertEqual(sorted(report["skipped"]), ["darling", "tasks"])

    def test_never_dispatched_area_gets_row_not_state_change(self):
        self._area("fresh")
        report = curator.run(self.db, self.cfg)
        self.assertEqual(report["stale"] + report["archived"], [])
        rows = {r["name"]: r for r in self.db.skill_usage_all()}
        self.assertEqual(rows["fresh"]["state"], "active")

    def test_never_used_old_row_ages_from_first_seen(self):
        self._area("ghost")
        self._seed("ghost", "active", last_used_days=None, first_seen_days=60)
        report = curator.run(self.db, self.cfg)
        self.assertEqual(report["stale"], ["ghost"])

    def test_archive_dir_invisible_to_registry(self):
        from dispatcher.areas import AreaRegistry
        self._area("expenses")
        self._seed("expenses", "stale", last_used_days=150)
        curator.run(self.db, self.cfg)
        self.assertEqual(AreaRegistry(self.root / "areas").load(), {})


if __name__ == "__main__":
    unittest.main()
