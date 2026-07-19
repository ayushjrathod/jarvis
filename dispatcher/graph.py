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


def extraction_prompt(db: Database, episodes_text: str, date: str) -> str:
    entities = db.entity_names(limit=200)
    candidates = db.active_facts(limit=80)
    return EXTRACT_PROMPT.format(
        date=date,
        episodes=episodes_text[:MAX_EPISODES_CHARS],
        entities=", ".join(entities) if entities else "(none yet)",
        candidates="\n".join(f"{f['id']}: {f['fact']}" for f in candidates)
        or "(none yet)",
    )


def reconcile_prompt(db: Database, date: str, limit: int = 200) -> str:
    facts = db.active_facts(limit=limit)
    return RECONCILE_PROMPT.format(
        date=date,
        facts="\n".join(f"{f['id']}: {f['fact']} · since {f['valid_at'] or '?'}"
                        for f in facts))


def apply_extraction(db: Database, obj: dict, episode_ids: list[int]) -> dict:
    """Deterministic apply of the extraction JSON; malformed items are
    skipped individually so one bad fact never voids the batch."""
    added, invalidated = 0, 0
    for item in obj.get("facts") or []:
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
    ids = [i for i in (obj.get("invalidated_ids") or []) if isinstance(i, int)]
    if ids:
        invalidated = db.invalidate_facts(ids)
    return {"facts_added": added, "invalidated": invalidated}


def apply_reconciliation(db: Database, obj: dict) -> dict:
    ids = [i for i in (obj.get("invalidate") or []) if isinstance(i, int)]
    return {"invalidated": db.invalidate_facts(ids) if ids else 0}


def parse_reply(answer: str) -> dict:
    return parse_response(answer)  # raises ValueError on garbage
