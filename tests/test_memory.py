"""Phase F memory tests: chunking, hash-diff ingest, FTS search, episodes,
blocks, consolidation hand-off. stdlib unittest, no network, no claude.
"""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from dispatcher import ingest, memory
from dispatcher.config import Config
from dispatcher.db import Database, fts_query
from dispatcher.ingest import chunk_markdown, extract_dates


def make_cfg(root: Path, **mem) -> Config:
    cfg = Config(root=root)
    cfg.memory = mem
    return cfg


class TestChunking(unittest.TestCase):
    def test_fenced_code_comments_are_not_headings(self):
        # A ```bash block containing "# install the thing" used to register
        # as a level-1 heading, evicting the real stack — every later chunk
        # carried the comment as ancestry (finding 2.8).
        text = ("# Real\nbody one\n\n```bash\n# install the thing\n"
                "sudo pacman -S x\n```\n\nbody two\n")
        chunks = chunk_markdown("vault/notes/a.md", text)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["heading"], "Real")
        self.assertIn("# install the thing", chunks[0]["compiled"])
        # unclosed fence: rest of file is code, no phantom headings either
        text2 = "# Real\nbody\n\n```\n# not a heading\nmore\n"
        chunks2 = chunk_markdown("f.md", text2)
        self.assertEqual(len(chunks2), 1)
        self.assertEqual(chunks2[0]["heading"], "Real")

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

    def test_unsupported_formats_are_counted_not_celebrated(self):
        # .csv/.eml have no chunker and were silently nobody's stat — while
        # the inbox announced them searchable.
        self.write("vault/inbox/expenses.csv", "a,b\n1,2\n")
        self.write("vault/inbox/thread.eml", "Subject: hi\n")
        stats = ingest.ingest_vault(self.db, self.root, ["vault"])
        self.assertEqual(stats["unsupported"], 2)
        self.assertEqual(stats["files_scanned"], 0)
        self.assertEqual(stats["added"], 0)

    def test_corrupt_file_records_mtime_and_stops_billing(self):
        # A chunker that raises used to leave the mtime unrecorded, so the
        # file re-paid full parsing on every reindex forever (up to 10 OCR
        # pages per inbox arrival). Now the mtime is recorded with zero
        # chunks: skipped until touched again.
        def boom(rel, p):
            raise RuntimeError("corrupt")

        self.write("vault/inbox/scan.pdf", "%PDF-garbage")
        with mock.patch.dict(ingest.CHUNKERS, {".pdf": boom}):
            stats = ingest.ingest_vault(self.db, self.root, ["vault"])
        self.assertEqual(stats["unparseable"], 1)
        self.assertIn("vault/inbox/scan.pdf", self.db.vault_file_mtimes())
        with mock.patch.dict(ingest.CHUNKERS, {".pdf": boom}):
            again = ingest.ingest_vault(self.db, self.root, ["vault"])
        self.assertEqual(again["files_changed"], 0)
        self.assertEqual(again["unparseable"], 0)

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


class TestDepMissingKeepsTheIndex(unittest.TestCase):
    """A parser dependency breaking must DEGRADE to counting, never delete a
    previously-good index.

    The realistic trigger is rapidocr_onnxruntime failing to import after an
    onnxruntime upgrade: every OCR'd document would silently leave search, and
    the only visible sign was a `deleted` count in a log nobody reads. The
    DepMissing handler used to discard the file from the `seen` set that the
    prune pass treats as "still on disk", so the prune deleted its entries —
    unlike the sibling OSError/Exception handlers, which never discarded.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "vault" / "notes").mkdir(parents=True)
        self.db = Database(self.root / "data" / "test.db")
        self.doc = self.root / "vault" / "notes" / "scan.md"
        self.doc.write_text("# Scanned\nthe quarterly invoice total\n")
        os.utime(self.doc, (1000, 1000))
        ingest.ingest_vault(self.db, self.root, ["vault"])
        self.assertEqual(len(self.db.search_entries("quarterly invoice")), 1)

    def tearDown(self):
        self.tmp.cleanup()

    def _reindex_with_broken_parser(self):
        """Touch the file (so it is re-parsed) with its chunker raising."""
        os.utime(self.doc, (2000, 2000))

        def broken(label, path):
            raise ingest.DepMissing("rapidocr_onnxruntime not installed")

        original = dict(ingest.CHUNKERS)
        ingest.CHUNKERS[".md"] = broken
        try:
            return ingest.ingest_vault(self.db, self.root, ["vault"])
        finally:
            ingest.CHUNKERS.clear()
            ingest.CHUNKERS.update(original)

    def test_entries_survive_a_missing_dep(self):
        stats = self._reindex_with_broken_parser()
        self.assertEqual(stats["dep_gated"], 1)
        self.assertEqual(stats["deleted"], 0)        # nothing was pruned
        # the observable outcome: the document is still searchable
        hits = self.db.search_entries("quarterly invoice")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["file_path"], "vault/notes/scan.md")

    def test_recovers_when_the_dep_comes_back(self):
        self._reindex_with_broken_parser()
        self.doc.write_text("# Scanned\nthe quarterly invoice total revised\n")
        os.utime(self.doc, (3000, 3000))
        ingest.ingest_vault(self.db, self.root, ["vault"])
        hits = self.db.search_entries("revised")
        self.assertEqual(len(hits), 1)

    def test_a_real_deletion_still_prunes(self):
        """The guard must not make the index un-prunable: a file that is gone
        from disk is still removed."""
        self.doc.unlink()
        stats = ingest.ingest_vault(self.db, self.root, ["vault"])
        self.assertEqual(stats["deleted"], 1)
        self.assertEqual(self.db.search_entries("quarterly invoice"), [])


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

    def test_machine_prompts_are_not_user_interactions(self):
        """media-parse/inbox-summarize had drifted off this one list while
        sitting on every other (2026-08-08). Live proof in data/mission.db,
        episode 62 — a media-parse *prompt* stored as the user's own words:
        "Convert the user's music request into a Spotify search… Reply with ONE
        JSON object", answered with a JSON blob, already consolidated. The
        nightly graph extractor has therefore read a format instruction as
        something the user said."""
        cfg = make_cfg(Path("."))
        for tt in ("media-parse", "inbox-summarize"):
            with self.subTest(task_type=tt):
                t = self._task(source=tt, metadata={"task_type": tt})
                self.assertFalse(memory.should_capture(cfg, t, "done"))

    def test_every_internal_task_type_is_excluded(self):
        cfg = make_cfg(Path("."))
        for tt in memory.INTERNAL_TASK_TYPES:
            with self.subTest(task_type=tt):
                self.assertFalse(memory.should_capture(
                    cfg, self._task(metadata={"task_type": tt}), "done"))

    def test_the_exclusion_list_cannot_silently_shrink(self):
        """Spelled out rather than derived: this list has now lost entries
        twice (daily-brief/weekly-review 2026-08-01, media-parse/
        inbox-summarize 2026-08-08), and each time the loss was invisible
        because every *remaining* entry still passed its test. Deleting a name
        from the constant must break a test. Adding one is a deliberate edit
        here — and a reminder to check the sibling lists in service.py
        (NO_CONTINUITY_SOURCES, DEFAULT_META_TASK_TYPES) and notify.py
        (NEVER_SURFACE_TASK_TYPES, NEVER_GATE_TASK_TYPES)."""
        self.assertEqual(set(memory.INTERNAL_TASK_TYPES), {
            "memory-consolidate", "reflection", "notify-gate",
            "automation-parse", "media-parse", "inbox-summarize",
            "graph-extract", "graph-reconcile", "daily-brief", "weekly-review",
            "divert"})

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

    def test_graph_hand_off_is_marked_separately_from_consolidation(self):
        """The two hand-offs fail independently, so they need separate marks.

        /memory/consolidate marks the batch consolidated and spawns BOTH the
        consolidation agent and graph extraction over it. A failed consolidation
        rolls `consolidated_at` back (the M6 fix) — which returns episodes the
        extractor already digested. Before `graph_extracted_at` the next pass
        re-extracted them, and only `add_fact`'s duplicate guard kept the graph
        from accumulating a copy of that night's facts per retry.
        """
        a = self.db.add_episode("t1", "voice", "quick", None, "done", "one", "1")
        b = self.db.add_episode("t2", "voice", "quick", None, "done", "two", "2")

        # first pass: both episodes are new to the graph
        self.assertEqual([e["id"] for e in self.db.episodes_needing_graph([a, b])],
                         [a, b])
        self.db.mark_episodes_consolidated([a, b])
        self.db.mark_episodes_graph_extracted([a, b])

        # the consolidation agent then fails, so its half rolls back…
        self.db.mark_episodes_unconsolidated([a, b])
        self.assertEqual(len(self.db.unconsolidated_episodes()), 2)
        # …and the next pass re-exports them for the agent, but the graph has
        # already read them and must not be handed them again.
        self.assertEqual(self.db.episodes_needing_graph([a, b]), [])

        # a genuinely new episode still reaches the graph
        c = self.db.add_episode("t3", "voice", "quick", None, "done", "three", "3")
        self.assertEqual([e["id"] for e in
                          self.db.episodes_needing_graph([a, b, c])], [c])

    def test_graph_export_renders_only_its_own_subset(self):
        a = self.db.add_episode("t1", "voice", "quick", None, "done", "alpha", "A")
        self.db.add_episode("t2", "voice", "quick", None, "done", "beta", "B")
        self.db.mark_episodes_graph_extracted([a])
        pending = self.db.episodes_needing_graph(
            [e["id"] for e in self.db.unconsolidated_episodes()])
        text = memory.render_episodes(pending)
        self.assertIn("beta", text)
        self.assertNotIn("alpha", text)

    def test_privileged_grants_in_the_agent_file_are_stripped(self):
        """H2 on the consolidation path.

        The consolidation task is submitted trusted=True, so its allowed_tools
        survive the H1 trust boundary intact — and the agent file lives under
        areas/**, which reflection and learn runs may edit. build_consolidation
        parsed that frontmatter itself and skipped the load-time sanitizer, so a
        prompt-injected reflection writing `- Bash` into consolidate.md handed
        the nightly run a shell.
        """
        agent = self.root / "areas" / "memory" / "agents" / "consolidate.md"
        agent.write_text(agent.read_text().replace(
            "allowed_tools:\n", "allowed_tools:\n  - Bash\n  - Write\n"))
        self.db.add_episode("t1", "voice", "quick", None, "done", "hi", "hello")
        tools = memory.build_consolidation(self.cfg, self.db)["metadata"]["allowed_tools"]
        self.assertNotIn("Bash", tools)
        self.assertNotIn("Write", tools)          # unscoped write, also stripped
        self.assertIn("Edit(vault/memory/**)", tools)   # the scoped grant stays

    def test_same_second_exports_do_not_clobber_each_other(self):
        """The export file is the ONLY replay artefact a failed consolidation
        has. `stamp` is one-second resolution and write_text overwrites, so two
        /memory/consolidate calls in the same second used to leave one export
        holding both batches' name and neither's guaranteed content."""
        # pin the clock so the collision is certain, not a race the test may lose
        with mock.patch.object(memory, "now",
                               return_value="2026-08-10T02:30:00+0000"):
            self.db.add_episode("t1", "voice", "quick", None, "done",
                                "first batch marker", "ok")
            first = memory.build_consolidation(self.cfg, self.db)
            self.db.mark_episodes_consolidated(first["episode_ids"])
            self.db.add_episode("t2", "voice", "quick", None, "done",
                                "second batch marker", "ok")
            second = memory.build_consolidation(self.cfg, self.db)

        self.assertNotEqual(first["export_path"], second["export_path"])
        self.assertIn("first batch marker",
                      (self.root / first["export_path"]).read_text())
        self.assertIn("second batch marker",
                      (self.root / second["export_path"]).read_text())
        # each prompt points at its OWN export, not a shared name
        self.assertIn(first["export_path"], first["text"])
        self.assertIn(second["export_path"], second["text"])


if __name__ == "__main__":
    unittest.main()
