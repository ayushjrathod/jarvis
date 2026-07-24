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

from .automations import parse_response  # generic strict-JSON extraction
from .db import Database

log = logging.getLogger("dispatcher.graph")

MAX_FACT_LEN = 300
MAX_ENTITIES_PER_FACT = 6
MAX_EPISODES_CHARS = 12000
# Active facts offered to the model as invalidation candidates (extraction /
# weekly reconcile). An apply may ONLY invalidate ids drawn from this set — a
# reply can never reach past the candidates it was shown (H3: no graph wipe).
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
    query the prompt builders use, so callers may thread it through or omit it."""
    return {f["id"] for f in db.active_facts(limit=limit)}


def extraction_prompt(db: Database, episodes_text: str, date: str) -> str:
    entities = db.entity_names(limit=200)
    candidates = db.active_facts(limit=CANDIDATE_LIMIT)
    return EXTRACT_PROMPT.format(
        date=date,
        episodes=episodes_text[:MAX_EPISODES_CHARS],
        entities=", ".join(entities) if entities else "(none yet)",
        candidates="\n".join(f"{f['id']}: {f['fact']}" for f in candidates)
        or "(none yet)",
    )


def reconcile_prompt(db: Database, date: str, limit: int = RECONCILE_LIMIT) -> str:
    facts = db.active_facts(limit=limit)
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


def apply_extraction(db: Database, obj: dict, episode_ids: list[int],
                     allowed_ids: set[int] | None = None) -> dict:
    """Deterministic apply of the extraction JSON; malformed items are
    skipped individually so one bad fact never voids the batch. Invalidation is
    restricted to `allowed_ids` (the candidates the prompt offered — re-derived
    at the current cap when not threaded through) and additions are capped."""
    if allowed_ids is None:
        allowed_ids = candidate_ids(db, CANDIDATE_LIMIT)
    added, invalidated = 0, 0
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
        valid_at = item.get("valid_at")
        valid_at = str(valid_at)[:10] if valid_at else None
        db.add_fact(fact, names[:MAX_ENTITIES_PER_FACT], valid_at=valid_at,
                    episode_ids=episode_ids)
        added += 1
    ids = _allowed_invalidations(obj.get("invalidated_ids"), allowed_ids)
    if ids:
        invalidated = db.invalidate_facts(ids)
    return {"facts_added": added, "invalidated": invalidated}


def apply_reconciliation(db: Database, obj: dict,
                         allowed_ids: set[int] | None = None) -> dict:
    if allowed_ids is None:
        allowed_ids = candidate_ids(db, RECONCILE_LIMIT)
    ids = _allowed_invalidations(obj.get("invalidate"), allowed_ids)
    return {"invalidated": db.invalidate_facts(ids) if ids else 0}


def parse_reply(answer: str) -> dict:
    return parse_response(answer)  # raises ValueError on garbage
