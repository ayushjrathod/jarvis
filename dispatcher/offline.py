"""Degraded answers when Claude is unreachable.

The plan-wide session cap has taken the assistant out twice (sessions 7 and
10): the quick path fails, speaks "session limit… resets at 2:30pm", and that's
the end of it until the window rolls over. But by then the box is holding an
indexed vault, an episode log and a fact graph — a lot of the questions asked
in a day are answerable from that alone, with no model in the loop.

So instead of only reporting the outage, a limited quick task falls back to the
Phase F/J hybrid search (BM25 + vectors, RRF-fused — the same call
`/memory/search` makes) and returns what it found, extractively. No LLM, no
generation, no paraphrase: the text is quoted verbatim from the vault with its
source file named, because the one thing worse than "I'm rate-limited" is a
confident sentence nobody wrote.

This is also the seam a local model would plug into later (§"local-model
fallback" in future/phone-access-and-feature-gaps.md) — that needs a dependency
and a user decision; this needs neither and is useful today.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("dispatcher.offline")

MAX_HITS = 3
SNIPPET_CHARS = 320
# Search words are what make this work at all; a query of nothing but stopwords
# retrieves noise, so we'd rather say we have nothing. Since 2026-08-08 the
# relevance anchor also runs per word, which means anything left in this list
# can anchor a question on its own — "about" and "did" were added then because
# both match this index by themselves (3 and 1 entries), and either one would
# have quietly re-opened the hole the anchor exists to close. Every other
# preposition and do-form was already here; those two were just missed.
STOPWORDS = frozenset("""
a about an and are as at be by can could did do does for from get give had has
have how i if in is it me my of on or should so tell that the their them then
there these this to was were what when where which who why will with would you
your
""".split())
MIN_CONTENT_WORDS = 1
# The anchor probes one content word at a time (see search_memory), so a
# rambling question could mean a lot of FTS round-trips. any() short-circuits on
# the first match, so the usual cost is one query; this only caps the miss case.
# A question whose ONLY matching term sits past the cap fails to anchor — that
# fails toward "I have nothing", which is the safe direction here.
ANCHOR_TERMS = 8


def _content_words(question: str) -> list[str]:
    words = re.findall(r"[a-z0-9']{2,}", (question or "").lower())
    return [w for w in words if w not in STOPWORDS]


# The core blocks (USER.md / MEMORY.md) open with an HTML-comment instruction
# to whoever edits them. It is the first thing in the chunk, so unstripped it
# eats the whole snippet and buries the actual fact underneath.
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def _clip(text: str) -> str:
    text = " ".join(_COMMENT_RE.sub(" ", text or "").split())
    if len(text) <= SNIPPET_CHARS:
        return text
    cut = text[:SNIPPET_CHARS]
    # prefer a sentence boundary so the quote doesn't end mid-word
    dot = cut.rfind(". ")
    return (cut[:dot + 1] if dot > SNIPPET_CHARS // 2 else cut.rstrip()) + "…"


def _source_of(entry: dict) -> str:
    path = (entry.get("file_path") or "").strip()
    name = path.rsplit("/", 1)[-1] if path else "memory"
    heading = (entry.get("heading") or "").strip()
    return f"{name} › {heading}" if heading else name


def search_memory(cfg, db, embeddings, question: str) -> list[dict]:
    """Hybrid vault+episode hits for the question, best first. Never raises —
    this runs on an error path and must not turn one failure into two."""
    if len(_content_words(question)) < MIN_CONTENT_WORDS:
        return []
    # Relevance anchor. Nearest-neighbour search ALWAYS returns something: ask
    # a small index about the airspeed velocity of a laden swallow and it hands
    # back its three least-unrelated chunks, which this function would then
    # quote under "here's what I already have on it". So require that BM25
    # matched at least one real term somewhere before offering anything.
    # Ranking still uses the hybrid path below, so vectors keep their job of
    # floating the right chunk up — they just can't conjure a topic from
    # nothing.
    #
    # Probe the content words ONE AT A TIME, not the whole question (fixed
    # 2026-08-08). db.fts_query joins terms with a space = FTS5 implicit AND, so
    # the old whole-question probe demanded that EVERY token — stopwords
    # included — occur in one chunk, and degraded mode was therefore dead for
    # anything phrased like a sentence. Measured against the live index:
    #   'what is my preferred coding tool' -> 0 hits   (so: "didn't find
    #   'preferred coding tool'            -> 0 hits    anything relevant",
    #   'Neovim'                           -> 1 hit     with the answer sitting
    #                                                   right there in USER.md)
    # CLAUDE.md cites "preferred coding tool" as the proof this feature works,
    # but that was verified through /memory/search — which never applies this
    # anchor. One word is one quoted token, so AND-of-one is exactly the
    # question being asked: did a real term match anything?
    #
    # Not db.fts_query_any over the raw question: OR semantics would let "the",
    # "of" or "a" anchor absolutely anything, which is the noise the anchor
    # exists to stop. Stopwords are dropped first, so the swallow still gets
    # nothing — none of airspeed/velocity/laden/swallow is in the index.
    try:
        if not any(db.search_entries(w, 1) or db.search_episodes(w, 1)
                   for w in _content_words(question)[:ANCHOR_TERMS]):
            return []
    except Exception:
        log.exception("offline: relevance anchor failed")
        return []

    qvec = None
    try:
        if embeddings.enabled(cfg) and db.vec_ok:
            qvec = embeddings.embed_query(cfg, question)
    except Exception:
        log.exception("offline: query embedding failed; using FTS only")
    hits: list[dict] = []
    try:
        for e in db.search_entries_hybrid(question, qvec, MAX_HITS):
            text = _clip(e.get("raw") or "")
            if text:
                hits.append({"source": _source_of(e), "text": text})
    except Exception:
        log.exception("offline: vault search failed")
    if len(hits) < MAX_HITS:
        try:
            for ep in db.search_episodes_hybrid(question, qvec, MAX_HITS):
                text = _clip(ep.get("assistant_text") or "")
                if text:
                    hits.append({
                        "source": f"an earlier answer ({ep.get('valid_at', '')[:10]})",
                        "text": text,
                    })
        except Exception:
            log.exception("offline: episode search failed")
    return hits[:MAX_HITS]


def compose(limit_speech: str, hits: list[dict]) -> str:
    """The degraded reply. Leads with the outage — the user must never think
    this is a normal answer — then quotes what the index actually holds."""
    if not hits:
        return (f"{limit_speech} I checked my memory for this and didn't find "
                f"anything relevant, so it'll have to wait.")
    lead = (f"{limit_speech} I can't think about it right now, but here's what "
            f"I already have on it:")
    parts = [f"• {h['text']}\n  — {h['source']}" for h in hits]
    return lead + "\n\n" + "\n\n".join(parts)
