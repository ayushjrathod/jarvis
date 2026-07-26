"""Degraded (rate-limited) answers from the local index — no LLM anywhere."""

import unittest
from unittest import mock

from dispatcher import offline


class _Emb:
    """Stand-in for dispatcher.embeddings."""

    def __init__(self, on=False, boom=False):
        self.on, self.boom = on, boom

    def enabled(self, cfg):
        return self.on

    def embed_query(self, cfg, q):
        if self.boom:
            raise RuntimeError("model missing")
        return b"vec"


class _Db:
    vec_ok = True

    def __init__(self, entries=(), episodes=(), fail=None, anchored=True):
        self.entries, self.episodes, self.fail = list(entries), list(episodes), fail
        self.anchored = anchored
        self.qvecs = []

    # the BM25 anchor: did any real term match anywhere?
    def search_entries(self, q, limit=10):
        if self.fail == "anchor":
            raise RuntimeError("fts exploded")
        return self.entries[:limit] if self.anchored else []

    def search_episodes(self, q, limit=10):
        if self.fail == "anchor":
            raise RuntimeError("fts exploded")
        return self.episodes[:limit] if self.anchored else []

    def search_entries_hybrid(self, q, qvec, limit):
        if self.fail == "entries":
            raise RuntimeError("fts exploded")
        self.qvecs.append(qvec)
        return self.entries[:limit]

    def search_episodes_hybrid(self, q, qvec, limit):
        if self.fail == "episodes":
            raise RuntimeError("fts exploded")
        return self.episodes[:limit]


ENTRY = {"file_path": "vault/notes/neovim.md", "heading": "Editors",
         "raw": "The preferred editor is Neovim, configured with lazy.nvim."}


class TestSearchMemory(unittest.TestCase):
    def test_returns_vault_hits_with_sources(self):
        hits = offline.search_memory({}, _Db([ENTRY]), _Emb(), "which editor do I use")
        self.assertEqual(len(hits), 1)
        self.assertIn("Neovim", hits[0]["text"])
        self.assertEqual(hits[0]["source"], "neovim.md › Editors")

    def test_falls_back_to_episodes_when_vault_is_thin(self):
        ep = {"assistant_text": "You said the deploy runs at 4pm.",
              "valid_at": "2026-07-20T10:00:00+00:00"}
        hits = offline.search_memory({}, _Db([], [ep]), _Emb(), "when is the deploy")
        self.assertEqual(len(hits), 1)
        self.assertIn("4pm", hits[0]["text"])
        self.assertIn("2026-07-20", hits[0]["source"])

    def test_stopword_only_question_retrieves_nothing(self):
        # "what is it" would pull noise out of any index
        self.assertEqual(offline.search_memory({}, _Db([ENTRY]), _Emb(),
                                               "what is it"), [])

    def test_uses_vectors_when_available(self):
        db = _Db([ENTRY])
        offline.search_memory({}, db, _Emb(on=True), "which editor")
        self.assertEqual(db.qvecs, [b"vec"])

    def test_embedding_failure_degrades_to_fts(self):
        db = _Db([ENTRY])
        hits = offline.search_memory({}, db, _Emb(on=True, boom=True), "which editor")
        self.assertEqual(db.qvecs, [None])      # FTS-only, not a crash
        self.assertEqual(len(hits), 1)

    def test_unrelated_question_gets_nothing_rather_than_noise(self):
        # KNN always returns *something*; without the BM25 anchor an index with
        # no matching term still hands back its three least-unrelated chunks,
        # which compose() would then quote as "what I already have on it"
        hits = offline.search_memory({}, _Db([ENTRY], [ENTRY], anchored=False),
                                     _Emb(on=True),
                                     "airspeed velocity of a laden swallow")
        self.assertEqual(hits, [])

    def test_anchor_failure_never_raises(self):
        self.assertEqual(
            offline.search_memory({}, _Db([ENTRY], fail="anchor"), _Emb(),
                                  "which editor"), [])

    def test_search_failure_never_raises(self):
        # this runs on an error path; one failure must not become two
        self.assertEqual(
            offline.search_memory({}, _Db([ENTRY], fail="entries"), _Emb(),
                                  "which editor"), [])
        self.assertEqual(
            offline.search_memory({}, _Db([], [], fail="episodes"), _Emb(),
                                  "which editor"), [])

    def test_html_comments_are_stripped(self):
        # USER.md/MEMORY.md open with an editor instruction in a comment; it
        # is first in the chunk and buries the actual fact if left in
        block = {"file_path": "vault/memory/USER.md", "heading": "User profile",
                 "raw": "<!-- one bullet per fact, absolute dates. Budget: 2000"
                        " chars. Edit freely. -->\n- Favourite editor is Neovim."}
        hits = offline.search_memory({}, _Db([block]), _Emb(), "favourite editor")
        self.assertNotIn("<!--", hits[0]["text"])
        self.assertNotIn("Budget", hits[0]["text"])
        self.assertTrue(hits[0]["text"].startswith("- Favourite editor is Neovim."))

    def test_snippets_are_clipped(self):
        long = {"file_path": "x.md", "raw": "word " * 500}
        hits = offline.search_memory({}, _Db([long]), _Emb(), "word search")
        self.assertLessEqual(len(hits[0]["text"]), offline.SNIPPET_CHARS + 1)
        self.assertTrue(hits[0]["text"].endswith("…"))

    def test_caps_the_number_of_hits(self):
        many = [dict(ENTRY, raw=f"entry {i}") for i in range(10)]
        hits = offline.search_memory({}, _Db(many, many), _Emb(), "entry search")
        self.assertEqual(len(hits), offline.MAX_HITS)


class TestCompose(unittest.TestCase):
    LIMIT = "Claude's session limit is hit right now. It resets at 2:30pm."

    def test_leads_with_the_outage(self):
        out = offline.compose(self.LIMIT, [{"source": "a.md", "text": "hello"}])
        self.assertTrue(out.startswith(self.LIMIT))

    def test_quotes_verbatim_with_attribution(self):
        out = offline.compose(self.LIMIT, [{"source": "neovim.md", "text": "Neovim."}])
        self.assertIn("Neovim.", out)
        self.assertIn("— neovim.md", out)

    def test_no_hits_says_so_rather_than_inventing(self):
        out = offline.compose(self.LIMIT, [])
        self.assertIn("didn't find anything relevant", out)
        self.assertTrue(out.startswith(self.LIMIT))


if __name__ == "__main__":
    unittest.main()
