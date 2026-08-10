"""Knowledge graph (Phase J2/J3): facts-as-sentences with bi-temporal
validity, extracted nightly from the same episode export the consolidation
agent reads, reconciled weekly.

Design after graphiti's bi-temporal edges and mem0's additive extraction /
ADD-UPDATE-DELETE reconciliation prompts (both Apache-2.0, patterns adapted):
one LLM call produces JSON, everything applied to SQLite is deterministic.
Facts link to entities n-ary (kg_fact_entities) rather than strict
source→target edges — simpler, and 1-hop expansion is a plain join.
valid_at = when the fact became true in the world; invalid_at/expired_at =
when it stopped / when we learned that. Invalidation never deletes.
"""

from __future__ import annotations

import logging
import re
from datetime import date as _date

from .automations import parse_response  # generic strict-JSON extraction
from .db import Database

log = logging.getLogger("dispatcher.graph")

MAX_FACT_LEN = 300
MAX_ENTITIES_PER_FACT = 6
MAX_EPISODES_CHARS = 12000
# Active facts offered to the model as invalidation candidates (extraction /
# weekly reconcile). An apply may ONLY invalidate ids drawn from this set — a
# reply can never reach past the candidates it was shown (H3: no graph wipe).
# The window is a WINDOW, not a prefix: db.candidate_facts rotates it so a fact
# outside the newest N is eventually offered instead of being immortal.
CANDIDATE_LIMIT = 80
RECONCILE_LIMIT = 200
# Facts one extraction reply may add — bounds a flood of the graph in one batch.
MAX_FACTS_PER_BATCH = 50

EXTRACT_PROMPT = """\
You extract durable facts from a day's interaction log into a personal \
knowledge graph. Today is {date}.

<episodes>
{episodes}
</episodes>

Known entities — reuse these exact names when the same thing is meant:
{entities}

Existing active facts (id: fact) — candidates for invalidation:
{candidates}

Reply with ONLY a JSON object, no prose, no fences:
{{"facts": [{{"fact": "<one declarative sentence, absolute dates, standalone>",
  "entities": ["<1-6 short entity names it involves>"],
  "valid_at": "YYYY-MM-DD" or null}}],
 "invalidated_ids": [<ids of existing facts these episodes contradict or supersede>]}}

Memory discipline: only durable facts worth recalling months from now —
preferences, people, projects, decisions, recurring patterns. No trivia, no
one-off task chatter, no assistant behavior notes. Prefer FEW good facts; an
empty facts list is a normal outcome. Entity names are short noun phrases
("Ayra", "mission-control", "Piper TTS")."""

RECONCILE_PROMPT = """\
You are the weekly reconciliation pass over a personal knowledge-graph fact \
store. Today is {date}.

Active facts (id: fact · since valid_at):
{facts}

Reply with ONLY a JSON object: {{"invalidate": [<fact ids>]}}
Invalidate only: exact/near duplicates (keep the better-worded copy), facts
contradicted by newer facts, and one-off states that clearly stopped being
true. When unsure, keep it. An empty list is the normal outcome."""


def candidate_ids(db: Database, limit: int = CANDIDATE_LIMIT) -> set[int]:
    """The exact set of active-fact ids offered as invalidation candidates —
    the only ids an apply is allowed to invalidate. Re-derivable from the same
    query the prompt builders use, so callers may thread it through or omit it.
    db.candidate_facts is deterministic precisely so those two calls agree."""
    return {f["id"] for f in db.candidate_facts(limit=limit)}


# Episodes in the export are "## Episode <id> — …" sections (memory.py writes
# them); anchoring at line start keeps a quoted heading inside a body from
# splitting a block.
EPISODE_RE = re.compile(r"^## Episode ", re.M)
NOTE_RESERVE = 120   # characters held back for the "N older … omitted" line


def clip_episodes(text: str, max_chars: int = MAX_EPISODES_CHARS) -> str:
    """Fit the episode export into the prompt budget on WHOLE-episode
    boundaries, keeping the most recent.

    The clip used to be a bare `episodes_text[:MAX_EPISODES_CHARS]` — silent,
    mid-sentence, and dropping the TAIL, i.e. the NEWEST episodes (the export is
    ordered by id). All 200 exported episodes are marked consolidated either
    way, so on a day with more than 12KB of interaction the newest ones never
    reached the graph and never would. Latent so far (exports run 0.2-5.8KB) but
    this codebase does not do silent caps, so it logs, keeps the recent end, and
    says in-band what it dropped (2026-08-10)."""
    if len(text) <= max_chars:
        return text
    starts = [m.start() for m in EPISODE_RE.finditer(text)]
    if not starts:
        # Unrecognized export shape: still keep the RECENT end rather than the
        # head, and say so — a truncation we can't do cleanly is not a reason to
        # do it wrongly.
        log.warning("graph: episode export (%d chars) has no episode headings; "
                    "clipping the oldest %d chars",
                    len(text), len(text) - max_chars)
        return text[-max_chars:]
    header = text[:starts[0]]
    blocks = [text[s:e] for s, e in zip(starts, starts[1:] + [len(text)])]
    # The omission note costs characters too; reserve them up front, or the
    # final trim would cut the tail off the NEWEST episode — the one thing this
    # whole function exists to keep.
    budget = max_chars - len(header) - NOTE_RESERVE
    kept: list[str] = []
    total = 0
    for b in reversed(blocks):
        if total + len(b) > budget:
            break
        kept.insert(0, b)
        total += len(b)
    if not kept and budget > 0:
        # one episode bigger than the whole budget: a truncated newest beats
        # handing the extractor an empty <episodes> block
        kept = [blocks[-1][:budget]]
    dropped = len(blocks) - len(kept)
    note = (f"[{dropped} older episode(s) omitted — the export exceeded the "
            f"{max_chars}-char prompt budget]\n\n")
    log.warning("graph: episode export %d chars over the %d-char budget; "
                "dropped the %d oldest of %d episodes",
                len(text), max_chars, dropped, len(blocks))
    return (header + note + "".join(kept))[:max_chars]


def extraction_prompt(db: Database, episodes_text: str, date: str) -> str:
    entities = db.entity_names(limit=200)
    candidates = db.candidate_facts(limit=CANDIDATE_LIMIT)
    return EXTRACT_PROMPT.format(
        date=date,
        episodes=clip_episodes(episodes_text),
        entities=", ".join(entities) if entities else "(none yet)",
        candidates="\n".join(f"{f['id']}: {f['fact']}" for f in candidates)
        or "(none yet)",
    )


def reconcile_prompt(db: Database, date: str, limit: int = RECONCILE_LIMIT) -> str:
    facts = db.candidate_facts(limit=limit)
    return RECONCILE_PROMPT.format(
        date=date,
        facts="\n".join(f"{f['id']}: {f['fact']} · since {f['valid_at'] or '?'}"
                        for f in facts))


def _allowed_invalidations(raw, allowed: set[int]) -> list[int]:
    """Clean a model-supplied id list: drop non-ints and bools
    (isinstance(True, int) is True — a bare [true] would otherwise mean id 1),
    de-dup, and keep only ids that were actually shown as candidates. Intersecting
    with `allowed` also caps the count at len(candidates)."""
    out, seen = [], set()
    for i in raw or []:
        if isinstance(i, bool) or not isinstance(i, int):
            continue
        if i in allowed and i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _valid_date(raw) -> str | None:
    """A YYYY-MM-DD `valid_at`, or None. Was `str(valid_at)[:10]`, which stored
    whatever the model said: a reply of "valid_at": "yesterday" went in verbatim
    and came back out of reconcile_prompt as "· since yesterday", i.e. an
    undated fact rendered as though it had a date, into the prompt that decides
    what to invalidate. Unparseable → drop the field, keep the fact
    (2026-08-10)."""
    if not raw:
        return None
    text = str(raw).strip()[:10]
    try:
        _date.fromisoformat(text)
    except ValueError:
        log.warning("graph: dropping unparseable valid_at %r", str(raw)[:40])
        return None
    return text


def apply_extraction(db: Database, obj: dict, episode_ids: list[int],
                     allowed_ids: set[int] | None = None) -> dict:
    """Deterministic apply of the extraction JSON; malformed items are
    skipped individually so one bad fact never voids the batch. Invalidation is
    restricted to `allowed_ids` (the candidates the prompt offered — re-derived
    at the current cap when not threaded through) and additions are capped."""
    if allowed_ids is None:
        allowed_ids = candidate_ids(db, CANDIDATE_LIMIT)
    added, invalidated, duplicates = 0, 0, 0
    for item in obj.get("facts") or []:
        if added >= MAX_FACTS_PER_BATCH:
            break
        if not isinstance(item, dict):
            continue
        fact = str(item.get("fact") or "").strip()
        names = [str(n).strip()[:60] for n in (item.get("entities") or [])
                 if str(n).strip()]
        if not fact or len(fact) > MAX_FACT_LEN or not names:
            continue
        if db.active_fact_id(fact) is not None:
            # Already in the graph and still active — see db.add_fact for how
            # the same episodes get re-extracted. Counted, not silent.
            duplicates += 1
            continue
        valid_at = _valid_date(item.get("valid_at"))
        db.add_fact(fact, names[:MAX_ENTITIES_PER_FACT], valid_at=valid_at,
                    episode_ids=episode_ids)
        added += 1
    ids = _allowed_invalidations(obj.get("invalidated_ids"), allowed_ids)
    if ids:
        invalidated = db.invalidate_facts(ids)
    # Rotate the candidate window only now: selection has to stay stable across
    # candidate_ids() + extraction_prompt(), so the stamp happens once the
    # window has actually been used (db.candidate_facts).
    db.mark_facts_offered(allowed_ids)
    out = {"facts_added": added, "invalidated": invalidated}
    if duplicates:
        out["duplicates_skipped"] = duplicates
        log.info("graph extraction: skipped %d duplicate active fact(s)",
                 duplicates)
    return out


def apply_reconciliation(db: Database, obj: dict,
                         allowed_ids: set[int] | None = None) -> dict:
    if allowed_ids is None:
        allowed_ids = candidate_ids(db, RECONCILE_LIMIT)
    ids = _allowed_invalidations(obj.get("invalidate"), allowed_ids)
    out = {"invalidated": db.invalidate_facts(ids) if ids else 0}
    db.mark_facts_offered(allowed_ids)   # rotate; see apply_extraction
    return out


def parse_reply(answer: str) -> dict:
    return parse_response(answer)  # raises ValueError on garbage
