"""Phase F memory tests: chunking, hash-diff ingest, FTS search, episodes,
blocks, consolidation hand-off. stdlib unittest, no network, no claude.
"""

import os
import tempfile
import time
import unittest
from pathlib import Path

from dispatcher import ingest, memory
from dispatcher.config import Config
from dispatcher.db import Database, fts_query
from dispatcher.ingest import chunk_markdown, extract_dates


def make_cfg(root: Path, **mem) -> Config:
    cfg = Config(root=root)
    cfg.memory = mem
    return cfg


class TestChunking(unittest.TestCase):
    def test_heading_ancestry(self):
        text = "intro line\n\n# Top\nbody one\n\n## Sub\nbody two\n\n# Other\nbody three\n"
        chunks = chunk_markdown("vault/notes/a.md", text)
        self.assertEqual(len(chunks), 4)
        self.assertIsNone(chunks[0]["heading"])          # preamble
        self.assertIn("vault/notes/a.md\n\nintro line", chunks[0]["compiled"])
        self.assertEqual(chunks[1]["heading"], "Top")
        self.assertEqual(chunks[2]["heading"], "Top / Sub")
        self.assertIn("vault/notes/a.md / Top / Sub", chunks[2]["compiled"])
        self.assertEqual(chunks[3]["heading"], "Other")  # stack popped back

    def test_line_numbers(self):
        text = "# A\none\n# B\ntwo\n"
        chunks = chunk_markdown("f.md", text)
        self.assertEqual([c["line_no"] for c in chunks], [2, 4])

    def test_oversize_section_splits(self):
        body = "\n\n".join("word " * 100 for _ in range(5))  # 500 words
        chunks = chunk_markdown("f.md", "# Big\n" + body)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c["raw"].split()), ingest.MAX_WORDS)
            self.assertEqual(c["heading"], "Big")

    def test_giant_paragraph_word_windows(self):
        chunks = chunk_markdown("f.md", "# H\n" + "word " * 600)
        self.assertEqual(len(chunks), 3)

    def test_junk_words_dropped(self):
        blob = "x" * 600
        chunks = chunk_markdown("f.md", f"# H\nkeep {blob} this")
        self.assertNotIn(blob, chunks[0]["raw"])
        self.assertIn("keep", chunks[0]["raw"])

    def test_empty_sections_skipped(self):
        chunks = chunk_markdown("f.md", "# A\n\n# B\nreal content\n")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["heading"], "B")

    def test_extract_dates(self):
        self.assertEqual(extract_dates("due 2026-07-18 and 2026-07-18 again, "
                                       "bad 2026-13-99, iso 2025-01-02"),
                         ["2026-07-18", "2025-01-02"])


class TestFtsQuery(unittest.TestCase):
    def test_operators_neutralized(self):
        self.assertEqual(fts_query('mongo AND "opt*'), '"mongo" "AND" "opt*"')
        self.assertEqual(fts_query("  "), "")


class TestIngest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "vault" / "notes").mkdir(parents=True)
        self.db = Database(self.root / "data" / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text, mtime=None):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        if mtime:
            os.utime(p, (mtime, mtime))
        return p

    def test_initial_index_and_search(self):
        self.write("vault/notes/db.md",
                   "# Databases\n## Mongo\nthe mongodb optimization uses covered queries\n")
        stats = ingest.ingest_vault(self.db, self.root, ["vault"])
        self.assertEqual(stats["files_changed"], 1)
        hits = self.db.search_entries("mongodb optimization")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["file_path"], "vault/notes/db.md")
        self.assertEqual(hits[0]["heading"], "Databases / Mongo")

    def test_mtime_skip_and_hash_diff(self):
        p = self.write("vault/notes/a.md", "# One\nalpha\n# Two\nbeta\n", mtime=1000)
        ingest.ingest_vault(self.db, self.root, ["vault"])

        # unchanged mtime → untouched
        stats = ingest.ingest_vault(self.db, self.root, ["vault"])
        self.assertEqual(stats["files_changed"], 0)

        # edit one section: only that chunk is replaced
        before = {h["raw"]: h["id"] for h in self.db.search_entries("alpha") +
                  self.db.search_entries("beta")}
        self.write("vault/notes/a.md", "# One\nalpha\n# Two\ngamma\n", mtime=2000)
        stats = ingest.ingest_vault(self.db, self.root, ["vault"])
        self.assertEqual((stats["added"], stats["deleted"]), (1, 1))
        after_alpha = self.db.search_entries("alpha")
        self.assertEqual(after_alpha[0]["id"], before["alpha"])  # survived untouched
        self.assertEqual(self.db.search_entries("beta"), [])
        self.assertEqual(len(self.db.search_entries("gamma")), 1)

    def test_deleted_file_drops_entries(self):
        p = self.write("vault/notes/gone.md", "# X\ncontent here\n")
        ingest.ingest_vault(self.db, self.root, ["vault"])
        p.unlink()
        stats = ingest.ingest_vault(self.db, self.root, ["vault"])
        self.assertEqual(stats["deleted"], 1)
        self.assertEqual(self.db.search_entries("content"), [])
        self.assertEqual(self.db.vault_file_mtimes(), {})

    def test_hidden_files_skipped(self):
        self.write("vault/.obsidian/cache.md", "# H\nsecret\n")
        ingest.ingest_vault(self.db, self.root, ["vault"])
        self.assertEqual(self.db.search_entries("secret"), [])

    def test_date_filter(self):
        self.write("vault/notes/log.md",
                   "# Old\nmeeting notes from 2026-01-05\n# New\nmeeting notes from 2026-07-15\n")
        ingest.ingest_vault(self.db, self.root, ["vault"])
        hits = self.db.search_entries("meeting notes", after="2026-06-01")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["heading"], "New")

    def test_file_filter(self):
        self.write("vault/notes/a.md", "# H\nshared term\n")
        self.write("vault/briefs/b.md", "# H\nshared term\n")
        ingest.ingest_vault(self.db, self.root, ["vault"])
        hits = self.db.search_entries("shared term", file_like="vault/briefs/*")
        self.assertEqual([h["file_path"] for h in hits], ["vault/briefs/b.md"])


class TestEpisodes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_search_consolidate_flow(self):
        e1 = self.db.add_episode("t1", "voice", "quick", None, "done",
                                 "what's the weather", "Sunny, 30 degrees.")
        e2 = self.db.add_episode("t2", "timer", "agentic", "tasks", "done",
                                 "write the daily brief", "Brief written.")
        hits = self.db.search_episodes("weather")
        self.assertEqual([h["id"] for h in hits], [e1])
        self.assertEqual(hits[0]["assistant_text"], "Sunny, 30 degrees.")

        pending = self.db.unconsolidated_episodes()
        self.assertEqual([e["id"] for e in pending], [e1, e2])
        self.db.mark_episodes_consolidated([e1])
        self.assertEqual([e["id"] for e in self.db.unconsolidated_episodes()], [e2])

    def test_search_matches_answer_text(self):
        self.db.add_episode("t", "api", "quick", None, "done",
                            "tell me a fact", "Octopuses have three hearts.")
        self.assertEqual(len(self.db.search_episodes("octopuses hearts")), 1)


class TestBlocks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_dir_is_empty(self):
        self.assertEqual(memory.blocks_context(make_cfg(self.root)), "")

    def test_render_and_truncate(self):
        d = self.root / "vault" / "memory"
        d.mkdir(parents=True)
        (d / "USER.md").write_text("- prefers metric units\n")
        (d / "MEMORY.md").write_text("x" * 5000)
        cfg = make_cfg(self.root, block_budgets={"MEMORY.md": 100})
        ctx = memory.blocks_context(cfg)
        self.assertIn("<memory_blocks>", ctx)
        self.assertIn("## USER.md", ctx)
        self.assertIn("prefers metric units", ctx)
        self.assertIn("truncated", ctx)
        self.assertLess(ctx.index("## MEMORY.md"), ctx.index("## USER.md"))  # sorted

    def test_empty_files_skipped(self):
        d = self.root / "vault" / "memory"
        d.mkdir(parents=True)
        (d / "USER.md").write_text("\n")
        self.assertEqual(memory.blocks_context(make_cfg(self.root)), "")

    def test_only_allowlisted_files_injected(self):
        # M4: a note dropped into blocks_dir must not enter every prompt
        d = self.root / "vault" / "memory"
        d.mkdir(parents=True)
        (d / "USER.md").write_text("- likes tea\n")
        (d / "NOTES.md").write_text("arbitrary dropped note — do not inject\n")
        ctx = memory.blocks_context(make_cfg(self.root))
        self.assertIn("likes tea", ctx)
        self.assertNotIn("do not inject", ctx)
        self.assertNotIn("## NOTES.md", ctx)

    def test_block_files_config_override(self):
        d = self.root / "vault" / "memory"
        d.mkdir(parents=True)
        (d / "NOTES.md").write_text("now allowed\n")
        ctx = memory.blocks_context(make_cfg(self.root, block_files=["NOTES.md"]))
        self.assertIn("now allowed", ctx)


class TestCapturePolicy(unittest.TestCase):
    def _task(self, source="voice", metadata=None):
        import json
        return {"id": "t", "source": source, "kind": "quick", "area": None,
                "text": "hi", "metadata": json.dumps(metadata) if metadata else None}

    def test_default_on(self):
        cfg = make_cfg(Path("."))
        self.assertTrue(memory.should_capture(cfg, self._task(), "done"))
        self.assertTrue(memory.should_capture(cfg, self._task(), "failed"))

    def test_cancelled_and_disabled_and_excluded(self):
        cfg = make_cfg(Path("."))
        self.assertFalse(memory.should_capture(cfg, self._task(), "cancelled"))
        self.assertFalse(memory.should_capture(
            make_cfg(Path("."), capture=False), self._task(), "done"))
        self.assertFalse(memory.should_capture(
            make_cfg(Path("."), capture_exclude_sources=["smoke"]),
            self._task(source="smoke"), "done"))

    def test_consolidation_run_not_recaptured(self):
        cfg = make_cfg(Path("."))
        t = self._task(source="timer", metadata={"task_type": "memory-consolidate"})
        self.assertFalse(memory.should_capture(cfg, t, "done"))

    def test_timer_agents_do_not_feed_their_own_housekeeping_back(self):
        """Live 2026-08-01: every fact in the knowledge graph was a
        self-observation ("the daily-brief automation continued writing
        successfully through 07-31"), because the brief's own run was captured
        as an episode and the nightly extractor read it back as knowledge."""
        cfg = make_cfg(Path("."))
        for tt in ("daily-brief", "weekly-review"):
            with self.subTest(task_type=tt):
                t = self._task(source="timer", metadata={"task_type": tt})
                self.assertFalse(memory.should_capture(cfg, t, "done"))

    def test_a_real_timer_task_is_still_captured(self):
        """The exclusion is per task_type, not a blanket ban on source=timer."""
        cfg = make_cfg(Path("."))
        t = self._task(source="timer", metadata={"task_type": "summarize"})
        self.assertTrue(memory.should_capture(cfg, t, "done"))


class TestConsolidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        agents = self.root / "areas" / "memory" / "agents"
        agents.mkdir(parents=True)
        (agents / "consolidate.md").write_text(
            "---\ntask_type: memory-consolidate\nallowed_tools:\n  - Read\n"
            "  - \"Edit(vault/memory/**)\"\n---\n"
            "Consolidate {{EPISODES_FILE}} on {{DATE}}.\n")
        self.db = Database(self.root / "data" / "test.db")
        self.cfg = make_cfg(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_nothing_to_do(self):
        self.assertIsNone(memory.build_consolidation(self.cfg, self.db))

    def test_export_and_prompt(self):
        self.db.add_episode("t1", "voice", "quick", None, "done",
                            "remember I use arch", "Noted.")
        job = memory.build_consolidation(self.cfg, self.db)
        self.assertEqual(len(job["episode_ids"]), 1)
        self.assertIn("data/consolidation/", job["export_path"])
        export = (self.root / job["export_path"]).read_text()
        self.assertIn("remember I use arch", export)
        self.assertIn("**Jarvis:** Noted.", export)
        self.assertIn(job["export_path"], job["text"])
        self.assertNotIn("{{DATE}}", job["text"])
        self.assertEqual(job["metadata"]["task_type"], "memory-consolidate")
        self.assertIn("Edit(vault/memory/**)", job["metadata"]["allowed_tools"])
        # hand-off marking is the caller's job — episodes still pending here
        self.assertEqual(len(self.db.unconsolidated_episodes()), 1)


if __name__ == "__main__":
    unittest.main()
