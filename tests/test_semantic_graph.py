"""Phase J tests: sqlite-vec store + RRF hybrid search (synthetic vectors —
no model download in unit tests) and the knowledge-graph store + extraction
apply. Vec tests skip when the sqlite-vec extension is unavailable."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from dispatcher import embeddings, graph
from dispatcher.db import Database, _rrf
from dispatcher.ingest import chunk_markdown

DIM = 384


def vec(hot: int) -> bytes:
    v = np.zeros(DIM, np.float32)
    v[hot] = 1.0
    return v.tobytes()


def _entries(db, path, text):
    db.replace_file_entries(path, 1.0, chunk_markdown(path, text))


class TestRRF(unittest.TestCase):
    def test_fusion_rewards_presence_in_both_lists(self):
        order, scores = _rrf([[1, 2, 3], [3, 4]])
        self.assertEqual(order[0], 3)  # in both lists
        self.assertGreater(scores[1], scores[4])  # rank 0 beats rank 1


class TestVecStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        if not self.db.vec_ok:
            self.skipTest("sqlite-vec unavailable")
        _entries(self.db, "a.md", "alpha notes about the vineyard project")
        _entries(self.db, "b.md", "completely unrelated cooking recipe")
        rows = self.db.entries_missing_embeddings(10)
        self.ids = {r["compiled"].split(".md")[0][-1]: r["id"] for r in rows}
        # a.md gets direction 0, b.md gets direction 1
        self.db.add_entry_embeddings([(self.ids["a"], vec(0)),
                                      (self.ids["b"], vec(1))])

    def tearDown(self):
        self.tmp.cleanup()

    def test_backfill_tracker_drains(self):
        self.assertEqual(self.db.entries_missing_embeddings(10), [])

    def test_hybrid_finds_semantic_hit_fts_misses(self):
        # query words match nothing in b.md, but the query vector points at it
        out = self.db.search_entries_hybrid("dinner ideas", vec(1), limit=5)
        self.assertTrue(any(r["file_path"] == "b.md" for r in out))

    def test_hybrid_fuses_both_signals(self):
        out = self.db.search_entries_hybrid("vineyard", vec(0), limit=5)
        self.assertEqual(out[0]["file_path"], "a.md")  # top in both lists
        self.assertIn("rrf", out[0])

    def test_no_qvec_degrades_to_fts(self):
        out = self.db.search_entries_hybrid("vineyard", None, limit=5)
        self.assertTrue(out)
        self.assertNotIn("rrf", out[0])

    def test_deletion_cleans_vectors(self):
        self.db.delete_file_entries("b.md")
        out = self.db.search_entries_hybrid("anything", vec(1), limit=5)
        self.assertFalse(any(r.get("file_path") == "b.md" for r in out))

    def test_episode_vectors_roundtrip(self):
        eid = self.db.add_episode("t", "voice", "quick", None, "done",
                                  "remember the vineyard", "ok")
        rows = self.db.episodes_missing_embeddings(10)
        self.assertEqual(rows[0]["id"], eid)
        self.db.add_episode_embeddings([(eid, vec(2))])
        out = self.db.search_episodes_hybrid("zzz nothing", vec(2), limit=5)
        self.assertTrue(any(r["id"] == eid for r in out))

    def test_add_entry_embeddings_skips_vanished_entry(self):
        # L3: an id with no backing entry must not be inserted as an orphan vec
        self.db.add_entry_embeddings([(9999, vec(3))])
        with self.db._conn() as c:
            n = c.execute("SELECT count(*) n FROM entries_vec"
                          " WHERE rowid=9999").fetchone()["n"]
        self.assertEqual(n, 0)

    def test_sweep_removes_orphan_vectors(self):
        # L3: an entry deleted out from under its vector is swept
        eid = self.ids["a"]
        with self.db._conn() as c:
            c.execute("DELETE FROM entries WHERE id=?", (eid,))  # orphans the vec
        self.assertEqual(self.db.sweep_orphan_vectors(), 1)
        with self.db._conn() as c:
            left = c.execute("SELECT count(*) n FROM entries_vec"
                             " WHERE rowid=?", (eid,)).fetchone()["n"]
        self.assertEqual(left, 0)

    def test_overlapping_passes_do_not_raise_on_vec0(self):
        # Finding 2.6: two backfill passes over the same missing list made the
        # loser's whole executemany abort on the first colliding rowid — vec0
        # RAISES on INSERT OR REPLACE — rolling back non-colliding rows too.
        eid = self.ids["a"]
        self.db.add_entry_embeddings([(eid, vec(0))])  # already embedded
        with self.db._conn() as c:
            n = c.execute("SELECT count(*) n FROM entries_vec"
                          " WHERE rowid=?", (eid,)).fetchone()["n"]
        self.assertEqual(n, 1)

    def test_concurrent_inserts_do_not_raise(self):
        import threading
        eid = self.ids["b"]
        errors = []

        def hammer():
            try:
                for _ in range(20):
                    self.db.add_entry_embeddings([(eid, vec(1))])
            except Exception as e:  # noqa: BLE001 — the test IS the handler
                errors.append(e)

        threads = [threading.Thread(target=hammer) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])


class TestEmbeddingIdentity(unittest.TestCase):
    """Finding 2.6: a different 384-dim model was accepted silently, so KNN
    answered from an incompatible space with no error anywhere — and a
    non-384-dim one failed every backfill while search kept serving stale
    vectors."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        if not self.db.vec_ok:
            self.skipTest("sqlite-vec unavailable")

    def tearDown(self):
        self.tmp.cleanup()

    def _cfg(self, model=embeddings.DEFAULT_MODEL):
        cfg = mock.Mock()
        cfg.embeddings = {"enabled": True, "model": model}
        return cfg

    def test_first_pass_stamps_identity_without_reset(self):
        eid = self.db.add_episode("t", "voice", "quick", None, "done", "q", "a")
        with mock.patch.object(embeddings, "embed_passages",
                               return_value=[vec(7)]) as ep:
            stats = embeddings.embed_missing(self._cfg(), self.db)
        self.assertTrue(ep.called)
        self.assertFalse(stats["model_reset"])
        self.assertEqual(self.db.embedding_identity(),
                         (embeddings.DEFAULT_MODEL, embeddings.DIM))
        self.assertEqual(self.db.episodes_missing_embeddings(10), [])

    def test_model_change_drops_and_rebuilds(self):
        eid = self.db.add_episode("t", "voice", "quick", None, "done", "q", "a")
        self.db.add_episode_embeddings([(eid, vec(7))])
        self.db.set_embedding_identity("sentence-transformers/all-MiniLM-L6-v2",
                                       embeddings.DIM)
        with mock.patch.object(embeddings, "embed_passages",
                               return_value=[vec(7)]):
            with self.assertLogs("dispatcher.embeddings", level="WARNING") as logs:
                stats = embeddings.embed_missing(self._cfg(), self.db)
        self.assertTrue(stats["model_reset"])
        self.assertTrue(any("model changed" in m for m in logs.output))
        self.assertEqual(self.db.embedding_identity(),
                         (embeddings.DEFAULT_MODEL, embeddings.DIM))
        # rebuilt from source text, not coexisting
        self.assertEqual(self.db.episodes_missing_embeddings(10), [])

    def test_same_model_never_resets(self):
        self.db.set_embedding_identity(embeddings.DEFAULT_MODEL, embeddings.DIM)
        with mock.patch.object(embeddings, "embed_passages",
                               return_value=[]) as ep:
            stats = embeddings.embed_missing(self._cfg(), self.db)
        self.assertFalse(stats["model_reset"])
        self.assertFalse(ep.called)


class TestOrphanVectorsDoNotEatResultSlots(unittest.TestCase):
    """A vec row whose backing entry is gone must not cost the caller a hit.

    Both hybrid searches sliced `order[:limit]` from the RRF fusion BEFORE
    fetching the backing rows, then dropped ids whose row had vanished — so an
    orphan vector (an entry deleted while vec_ok was False, the L3 window
    sweep_orphan_vectors exists to close) silently consumed a result slot: ask
    for 2, get 1, nothing logged, the next-best real hit still sitting unread
    in `order`.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        if not self.db.vec_ok:
            self.skipTest("sqlite-vec unavailable")

    def tearDown(self):
        self.tmp.cleanup()

    def _orphan_entries(self):
        for i, name in enumerate("abc"):
            _entries(self.db, f"{name}.md", f"note number {i} about things")
        ids = {r["compiled"].split(".md")[0][-1]: r["id"]
               for r in self.db.entries_missing_embeddings(10)}
        self.db.add_entry_embeddings([(ids[n], vec(i))
                                      for i, n in enumerate("abc")])
        with self.db._conn() as c:      # entry gone, its vector left behind
            c.execute("DELETE FROM entries WHERE id=?", (ids["a"],))
        return ids

    def test_entries_hybrid_still_returns_limit_live_rows(self):
        self._orphan_entries()
        # no FTS hits, so ranking is pure KNN and the orphan is nearest
        out = self.db.search_entries_hybrid("zzz nothing matches", vec(0), limit=2)
        self.assertEqual(len(out), 2)
        self.assertEqual({r["file_path"] for r in out}, {"b.md", "c.md"})
        self.assertTrue(all("rrf" in r for r in out))

    def test_orphan_never_appears_in_results(self):
        self._orphan_entries()
        out = self.db.search_entries_hybrid("zzz nothing matches", vec(0), limit=5)
        self.assertEqual(len(out), 2)          # only two live entries exist
        self.assertNotIn("a.md", [r.get("file_path") for r in out])

    def test_episodes_hybrid_still_returns_limit_live_rows(self):
        eids = [self.db.add_episode(f"t{i}", "voice", "quick", None, "done",
                                    f"episode number {i}", "ok")
                for i in range(3)]
        self.db.add_episode_embeddings([(e, vec(i)) for i, e in enumerate(eids)])
        with self.db._conn() as c:
            c.execute("DELETE FROM episodes WHERE id=?", (eids[0],))
        out = self.db.search_episodes_hybrid("zzz nothing matches", vec(0), limit=2)
        self.assertEqual(len(out), 2)
        self.assertEqual({r["id"] for r in out}, set(eids[1:]))


class TestGraphStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_entity_upsert_is_case_insensitive(self):
        a = self.db.upsert_entity("Piper TTS")
        b = self.db.upsert_entity("piper tts")
        self.assertEqual(a, b)

    def test_question_words_do_not_starve_fact_search(self):
        """Facts are single sentences: OR semantics, else 'where does mira
        live' can never match 'Mira lives in Pune.' (found live)."""
        self.db.add_fact("Ayra's sister Mira lives in Pune.", ["Ayra", "Mira"])
        hits = self.db.search_facts("where does mira live")
        self.assertTrue(hits)
        self.assertIn("Pune", hits[0]["fact"])

    def test_fact_lifecycle(self):
        fid = self.db.add_fact("Ayra prefers Neovim.", ["Ayra", "Neovim"],
                               valid_at="2026-07-18", episode_ids=[1, 2])
        hits = self.db.search_facts("neovim")
        self.assertEqual(hits[0]["id"], fid)
        self.assertIn("Ayra", hits[0]["entities"])

        self.assertEqual(self.db.invalidate_facts([fid]), 1)
        self.assertEqual(self.db.invalidate_facts([fid]), 0)  # already done
        self.assertFalse(self.db.search_facts("neovim"))
        self.assertTrue(self.db.search_facts("neovim", include_invalid=True))
        self.assertEqual(self.db.graph_counts()["facts_invalidated"], 1)

    def test_invalidation_keeps_record_time_and_world_time_distinct(self):
        old = self.db.add_fact("Ayra uses Vim.", ["Ayra"], valid_at="2026-07-01")
        self.assertEqual(self.db.invalidate_facts([old], superseded_at="2026-07-18"), 1)
        with self.db._conn() as c:
            row = c.execute("SELECT invalid_at, expired_at FROM kg_facts WHERE id=?",
                            (old,)).fetchone()
        self.assertEqual(row["expired_at"], "2026-07-18")  # world time: the new fact
        self.assertNotEqual(row["invalid_at"], "2026-07-18")  # record time: now-ish
        self.assertTrue(row["invalid_at"] >= "2026-07-18")

    def test_newer_knowledge_survives_older_supersession(self):
        # The model naming an id is a suggestion: a candidate NEWER than the
        # superseding knowledge must not be killed by it.
        new = self.db.add_fact("Ayra uses Neovim.", ["Ayra"], valid_at="2026-08-01")
        self.assertEqual(self.db.invalidate_facts([new], superseded_at="2026-07-18"), 0)
        self.assertTrue(self.db.search_facts("neovim"))
        undated = self.db.add_fact("Ayra edits text.", ["Ayra"])
        self.assertEqual(self.db.invalidate_facts([undated], superseded_at="2026-07-18"), 1)

    def test_extraction_threads_the_new_date_into_retirement(self):
        old = self.db.add_fact("Ayra uses Vim.", ["Ayra"], valid_at="2026-07-01")
        counts = graph.apply_extraction(self.db, {
            "facts": [{"fact": "Ayra switched to Neovim.",
                       "entities": ["Ayra", "Neovim"], "valid_at": "2026-07-18"}],
            "invalidated_ids": [old],
        }, episode_ids=[7])
        self.assertEqual(counts, {"facts_added": 1, "invalidated": 1})
        with self.db._conn() as c:
            row = c.execute("SELECT expired_at FROM kg_facts WHERE id=?",
                            (old,)).fetchone()
        self.assertEqual(row["expired_at"], "2026-07-18")

    def test_one_hop_neighbors(self):
        f1 = self.db.add_fact("Ayra runs mission-control.", ["Ayra", "mission-control"])
        f2 = self.db.add_fact("mission-control runs on Arch.", ["mission-control", "Arch"])
        f3 = self.db.add_fact("Unrelated fact.", ["Nobody"])
        near = {f["id"] for f in self.db.fact_neighbors([f1])}
        self.assertIn(f2, near)
        self.assertNotIn(f3, near)
        self.assertNotIn(f1, near)

    def test_add_fact_is_atomic_on_failure(self):
        # L1: a failure partway through must leave no partial fact — no orphan
        # kg_facts row, no dangling FTS row.
        from unittest.mock import patch
        with patch.object(Database, "_upsert_entity", side_effect=ValueError("boom")):
            with self.assertRaises(ValueError):
                self.db.add_fact("Half-written fact.", ["X"])
        counts = self.db.graph_counts()
        self.assertEqual(counts["facts_active"], 0)
        self.assertFalse(self.db.search_facts("Half-written"))

    def test_duplicate_active_fact_is_not_re_added(self):
        """add_fact was an unconditional INSERT, and the graph has a standing
        way to see the same episodes twice: /memory/consolidate marks a batch,
        spawns BOTH the consolidation agent and graph extraction over it, and a
        failed consolidation calls mark_episodes_unconsolidated (the M6 fix) —
        returning to the pool episodes the extractor already read. The next
        nightly pass re-extracted them and permanently duplicated that night's
        facts, one copy per retry."""
        first = self.db.add_fact("Ayra prefers Neovim.", ["Ayra", "Neovim"])
        again = self.db.add_fact("Ayra prefers Neovim.", ["Ayra"])
        self.assertEqual(again, first)                   # same row, no insert
        self.assertEqual(self.db.graph_counts()["facts_active"], 1)
        self.assertEqual(len(self.db.search_facts("Neovim")), 1)

    def test_dedup_does_not_resurrect_an_invalidated_fact(self):
        """Only ACTIVE duplicates are rejected: a fact that stopped being true
        and later becomes true again is a legitimate new row, and reusing the
        old one would erase the bi-temporal history."""
        first = self.db.add_fact("Ayra lives in Pune.", ["Ayra"])
        self.db.invalidate_facts([first])
        second = self.db.add_fact("Ayra lives in Pune.", ["Ayra"])
        self.assertNotEqual(second, first)
        counts = self.db.graph_counts()
        self.assertEqual((counts["facts_active"], counts["facts_invalidated"]),
                         (1, 1))

    def test_busy_timeout_pragma_applied(self):
        # M3: every connection gets a generous busy_timeout so WAL contention
        # waits at SQLite instead of raising OperationalError at the driver default.
        from dispatcher.db import BUSY_TIMEOUT_S
        with self.db._conn() as c:
            got = c.execute("PRAGMA busy_timeout").fetchone()[0]
        self.assertEqual(got, BUSY_TIMEOUT_S * 1000)


class TestApplyExtraction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_apply_and_invalidate(self):
        old = self.db.add_fact("Ayra uses Vim.", ["Ayra"])
        counts = graph.apply_extraction(self.db, {
            "facts": [
                {"fact": "Ayra switched to Neovim on 2026-07-18.",
                 "entities": ["Ayra", "Neovim"], "valid_at": "2026-07-18"},
                {"fact": "", "entities": ["x"]},          # skipped: empty
                {"fact": "No entities."},                  # skipped: no entities
                "not a dict",                              # skipped
            ],
            "invalidated_ids": [old, 99999, "bogus"],
        }, episode_ids=[7])
        self.assertEqual(counts, {"facts_added": 1, "invalidated": 1})
        self.assertEqual(self.db.graph_counts()["facts_active"], 1)

    def test_reconciliation_apply(self):
        f1 = self.db.add_fact("Dup A.", ["X"])
        self.db.add_fact("Dup B.", ["X"])
        out = graph.apply_reconciliation(self.db, {"invalidate": [f1]})
        self.assertEqual(out, {"invalidated": 1})

    def test_invalidation_restricted_to_candidates(self):
        """A reply may only invalidate ids it was shown as candidates; an active
        fact absent from the candidate set (or a bogus id) is left untouched."""
        a = self.db.add_fact("Candidate fact.", ["X"])
        b = self.db.add_fact("Active but not a candidate.", ["Y"])
        counts = graph.apply_extraction(
            self.db, {"invalidated_ids": [a, b, 4242]},
            episode_ids=[1], allowed_ids={a})
        self.assertEqual(counts["invalidated"], 1)      # only a
        self.assertTrue(self.db.search_facts("candidate", include_invalid=True))
        self.assertTrue(self.db.search_facts("active"))  # b still active
        self.assertEqual(self.db.graph_counts()["facts_active"], 1)

    def test_extraction_cannot_soft_wipe_the_graph(self):
        """H3 repro: a reply enumerating a huge id range invalidates only the
        real active candidates, not every id it names."""
        a = self.db.add_fact("Keep me.", ["X"])
        b = self.db.add_fact("Keep me too.", ["Y"])
        counts = graph.apply_extraction(
            self.db, {"invalidated_ids": list(range(1, 10000))},
            episode_ids=[1], allowed_ids={a})  # b not offered as a candidate
        self.assertEqual(counts["invalidated"], 1)
        self.assertEqual(self.db.graph_counts()["facts_active"], 1)
        self.assertTrue(self.db.search_facts("keep me too"))  # b survives
        _ = b

    def test_bools_are_not_fact_ids(self):
        """isinstance(True, int) is True; a bare [true]/[false] must invalidate
        nothing even though True == 1 could collide with fact id 1."""
        a = self.db.add_fact("Fact number one.", ["X"])  # id 1 on a fresh db
        counts = graph.apply_extraction(
            self.db, {"invalidated_ids": [True, False]},
            episode_ids=[1], allowed_ids={a})
        self.assertEqual(counts["invalidated"], 0)
        self.assertEqual(self.db.graph_counts()["facts_active"], 1)

    def test_reconciliation_restricted_to_candidates(self):
        a = self.db.add_fact("Reconcile me.", ["X"])
        b = self.db.add_fact("Off the list.", ["Y"])
        out = graph.apply_reconciliation(
            self.db, {"invalidate": [a, b]}, allowed_ids={a})
        self.assertEqual(out, {"invalidated": 1})
        self.assertEqual(self.db.graph_counts()["facts_active"], 1)
        _ = b

    def test_facts_batch_is_capped(self):
        many = [{"fact": f"Durable fact number {i}.", "entities": ["X"]}
                for i in range(graph.MAX_FACTS_PER_BATCH + 20)]
        counts = graph.apply_extraction(self.db, {"facts": many},
                                        episode_ids=[1])
        self.assertEqual(counts["facts_added"], graph.MAX_FACTS_PER_BATCH)
        self.assertEqual(self.db.graph_counts()["facts_active"],
                         graph.MAX_FACTS_PER_BATCH)

    def test_parse_reply_garbage_raises(self):
        with self.assertRaises(ValueError):
            graph.parse_reply("no json at all")

    def test_prompts_carry_context(self):
        self.db.add_fact("Existing fact.", ["Ayra"])
        p = graph.extraction_prompt(self.db, "EPISODE TEXT", "2026-07-19")
        self.assertIn("EPISODE TEXT", p)
        self.assertIn("Existing fact.", p)
        self.assertIn("Ayra", p)
        r = graph.reconcile_prompt(self.db, "2026-07-19")
        self.assertIn("Existing fact.", r)

    def test_re_extraction_of_the_same_batch_does_not_duplicate(self):
        """The rolled-back-consolidation path, end to end: the identical
        extraction reply applied twice must leave one fact, and say so."""
        reply = {"facts": [{"fact": "Ayra runs Arch Linux.",
                            "entities": ["Ayra", "Arch Linux"]}]}
        first = graph.apply_extraction(self.db, dict(reply), episode_ids=[1, 2])
        second = graph.apply_extraction(self.db, dict(reply), episode_ids=[1, 2])
        self.assertEqual(first["facts_added"], 1)
        self.assertEqual(second["facts_added"], 0)
        self.assertEqual(second["duplicates_skipped"], 1)
        self.assertEqual(self.db.graph_counts()["facts_active"], 1)

    def test_unparseable_valid_at_is_dropped_not_stored(self):
        """`str(valid_at)[:10]` stored whatever the model said, so a reply of
        "valid_at": "yesterday" came back out of reconcile_prompt as
        "· since yesterday" — an undated fact rendered as dated, into the prompt
        that decides what to invalidate."""
        graph.apply_extraction(self.db, {"facts": [
            {"fact": "Ayra started the rewrite.", "entities": ["Ayra"],
             "valid_at": "yesterday"},
            {"fact": "Ayra bought a keyboard.", "entities": ["Ayra"],
             "valid_at": "2026-08-09"},
            {"fact": "Ayra adopted a cat.", "entities": ["Ayra"],
             "valid_at": "2026-13-45"},        # well-formed but not a date
        ]}, episode_ids=[1])
        by_fact = {f["fact"]: f["valid_at"] for f in self.db.active_facts()}
        self.assertIsNone(by_fact["Ayra started the rewrite."])
        self.assertIsNone(by_fact["Ayra adopted a cat."])
        self.assertEqual(by_fact["Ayra bought a keyboard."], "2026-08-09")
        # and the junk never reaches the next prompt
        r = graph.reconcile_prompt(self.db, "2026-08-10")
        self.assertNotIn("yesterday", r)
        self.assertIn("· since ?", r)

    def test_valid_at_keeps_the_date_from_a_timestamp(self):
        graph.apply_extraction(self.db, {"facts": [
            {"fact": "Dated fact.", "entities": ["X"],
             "valid_at": "2026-08-09T14:30:00Z"}]}, episode_ids=[1])
        self.assertEqual(self.db.active_facts()[0]["valid_at"], "2026-08-09")


class TestCandidateWindowRotates(unittest.TestCase):
    """Every active fact must eventually become an invalidation candidate.

    The window was `ORDER BY id DESC LIMIT ?`, so once the graph held more than
    80 (extraction) / 200 (reconcile) active facts, everything older was
    IMMORTAL: neither pass was ever shown it again, and invalidation is the only
    removal mechanism ("never deletes"), so a contradicted old fact kept ranking
    in /memory/search?scope=graph forever.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.ids = [self.db.add_fact(f"Durable fact number {i}.", ["X"])
                    for i in range(1, 7)]

    def tearDown(self):
        self.tmp.cleanup()

    def test_every_fact_is_eventually_offered(self):
        """The regression: with a window of 2 over 6 facts, the old query
        returned the same two ids forever."""
        offered = set()
        for _ in range(6):
            window = graph.candidate_ids(self.db, limit=2)
            offered |= window
            # what the real path does once the window has been used
            graph.apply_extraction(self.db, {}, episode_ids=[],
                                   allowed_ids=window)
        self.assertEqual(offered, set(self.ids))

    def test_newest_facts_stay_in_the_window(self):
        """Half the window is still reserved for recent facts — today's
        episodes usually contradict something recent."""
        for _ in range(4):
            window = graph.candidate_ids(self.db, limit=2)
            self.assertIn(self.ids[-1], window)
            self.db.mark_facts_offered(window)

    def test_oldest_offered_comes_back_first(self):
        stamps = ["2026-08-09T00:00:00+0000", "2026-08-06T00:00:00+0000",
                  "2026-08-08T00:00:00+0000", "2026-08-07T00:00:00+0000"]
        with self.db._conn() as c:
            for fid, ts in zip(self.ids, stamps):
                c.execute("UPDATE kg_facts SET last_offered_at=? WHERE id=?",
                          (ts, fid))
        # recent=0 isolates the rotating half
        rotating = self.db.candidate_facts(limit=2, recent=0)
        self.assertEqual([f["id"] for f in rotating],
                         [self.ids[4], self.ids[5]])   # never offered (NULL) first
        rotating = self.db.candidate_facts(limit=4, recent=0)
        self.assertEqual([f["id"] for f in rotating][2:],
                         [self.ids[1], self.ids[3]])   # then 08-06, then 08-07

    def test_selection_is_stable_between_calls(self):
        """service.py derives allowed_ids and builds the prompt in two separate
        calls. If they disagreed, the model's verdict on a fact it WAS shown
        would be silently dropped by the anti-wipe guard."""
        a = graph.candidate_ids(self.db, limit=3)
        b = {f["id"] for f in self.db.candidate_facts(limit=3)}
        self.assertEqual(a, b)

    def test_rotation_never_offers_an_invalidated_fact(self):
        self.db.invalidate_facts(self.ids[:3])
        offered = set()
        for _ in range(6):
            window = graph.candidate_ids(self.db, limit=2)
            offered |= window
            self.db.mark_facts_offered(window)
        self.assertEqual(offered, set(self.ids[3:]))

    def test_anti_wipe_guard_still_holds_over_a_rotated_window(self):
        """H3 must survive the rotation: an apply may still only invalidate ids
        it was actually shown."""
        window = graph.candidate_ids(self.db, limit=2)
        counts = graph.apply_extraction(
            self.db, {"invalidated_ids": list(range(1, 500))},
            episode_ids=[1], allowed_ids=window)
        self.assertEqual(counts["invalidated"], 2)
        self.assertEqual(self.db.graph_counts()["facts_active"], 4)


class TestEpisodeClipping(unittest.TestCase):
    """Graph extraction clipped the export at 12000 chars with a bare slice —
    silent, mid-sentence, and dropping the TAIL, i.e. the NEWEST episodes (the
    export is ordered by id). All 200 exported episodes are marked consolidated
    either way, so on a busy day the newest ones never reached the graph and
    never would."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _export(self, n: int, body_chars: int) -> str:
        parts = ["# Episode export — 2026-08-10T02:30:00+0000", ""]
        for i in range(1, n + 1):
            parts += [f"## Episode {i} — 2026-08-10 · voice · quick · done",
                      f"**User:** marker{i} " + "x" * body_chars, ""]
        return "\n".join(parts)

    def test_under_budget_is_untouched(self):
        text = self._export(3, 50)
        self.assertEqual(graph.clip_episodes(text, 100000)[0], text)

    def test_keeps_the_newest_episodes_not_the_oldest(self):
        text = self._export(10, 400)
        out, _seen = graph.clip_episodes(text, 1500)
        self.assertLessEqual(len(out), 1500)
        self.assertIn("marker10", out)       # the newest survived
        self.assertNotIn("marker1 ", out)    # the oldest did not

    def test_clips_on_whole_episode_boundaries(self):
        out, _seen = graph.clip_episodes(self._export(10, 400), 1500)
        blocks = out.split("## Episode ")[1:]
        self.assertTrue(blocks)
        for b in blocks:                     # no half-parsed episode survives
            self.assertIn("**User:** marker", b)

    def test_the_clip_is_announced_in_band_and_logged(self):
        with self.assertLogs("dispatcher.graph", level="WARNING") as cm:
            out, _seen = graph.clip_episodes(self._export(10, 400), 1500)
        self.assertIn("older episode(s) omitted", out)
        self.assertTrue(any("over the" in m for m in cm.output))

    def test_result_never_exceeds_the_budget(self):
        for budget in (400, 900, 1500, 3000):
            with self.subTest(budget=budget):
                with self.assertLogs("dispatcher.graph", level="WARNING"):
                    out, _seen = graph.clip_episodes(self._export(10, 400), budget)
                self.assertLessEqual(len(out), budget)

    def test_a_single_oversized_episode_is_truncated_not_dropped(self):
        """Handing the extractor an empty <episodes> block would be worse than
        a truncated one."""
        with self.assertLogs("dispatcher.graph", level="WARNING"):
            out, _seen = graph.clip_episodes(self._export(2, 5000), 900)
        self.assertLessEqual(len(out), 900)
        self.assertIn("marker2", out)          # the newest, truncated
        self.assertNotIn("marker1 ", out)

    def test_unrecognized_shape_still_keeps_the_recent_end(self):
        text = "oldest marker\n" + "y" * 5000 + "\nnewest marker"
        with self.assertLogs("dispatcher.graph", level="WARNING"):
            out, seen = graph.clip_episodes(text, 200)
        self.assertIn("newest marker", out)
        self.assertNotIn("oldest marker", out)
        self.assertEqual(seen, [])  # nothing parseable: mark nothing, retry

    def test_clip_reports_which_episodes_survived(self):
        # The service marks only these extracted: marking the full list
        # orphaned ~178 of 200 episodes past the budget on every backlog.
        _out, seen = graph.clip_episodes(self._export(10, 400), 1500)
        for i in seen:
            self.assertIn(f"marker{i} ", _out)
        self.assertNotIn(1, seen)  # oldest dropped
        self.assertIn(10, seen)  # newest kept
        _out2, seen2 = graph.clip_episodes(self._export(3, 50), 100000)
        self.assertEqual(seen2, [1, 2, 3])  # under budget: all seen

    def test_extraction_prompt_applies_the_clip(self):
        with self.assertLogs("dispatcher.graph", level="WARNING"):
            p = graph.extraction_prompt(self.db, self._export(60, 400),
                                        "2026-08-10")
        self.assertIn("marker60", p)
        self.assertIn("older episode(s) omitted", p)


if __name__ == "__main__":
    unittest.main()
