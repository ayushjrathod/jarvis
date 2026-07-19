"""Phase J tests: sqlite-vec store + RRF hybrid search (synthetic vectors —
no model download in unit tests) and the knowledge-graph store + extraction
apply. Vec tests skip when the sqlite-vec extension is unavailable."""

import tempfile
import unittest
from pathlib import Path

import numpy as np

from dispatcher import graph
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

    def test_one_hop_neighbors(self):
        f1 = self.db.add_fact("Ayra runs mission-control.", ["Ayra", "mission-control"])
        f2 = self.db.add_fact("mission-control runs on Arch.", ["mission-control", "Arch"])
        f3 = self.db.add_fact("Unrelated fact.", ["Nobody"])
        near = {f["id"] for f in self.db.fact_neighbors([f1])}
        self.assertIn(f2, near)
        self.assertNotIn(f3, near)
        self.assertNotIn(f1, near)


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


if __name__ == "__main__":
    unittest.main()
