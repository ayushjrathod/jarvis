---
name: memory
description: Persistent memory blocks in vault/memory/ (Phase F)
# Agentic triggers: the user is telling Jarvis to change what it remembers.
triggers:
  - remember that
  - remember this
  - forget that
  - forget what
  - update your memory
# Quick triggers: read-only recall, streamed to voice.
quick_triggers:
  - what do you remember
  - what do you know about
  - search your memory
  - search memory
allowed_tools:
  - Read
  - Glob
  - Grep
  # Edit(path) rules cover Write too; a "Write(path)" rule is silently ignored
  - "Edit(vault/memory/**)"
quick_allowed_tools:
  - Read
  - Glob
  - Grep
---
# Memory area

You maintain Jarvis's persistent memory: plain markdown block files in
`vault/memory/`. These blocks are injected into every prompt, so they must
stay small, current, and true. Never touch files outside `vault/memory/`.

## Block files

- `USER.md` — facts about the user: preferences, people, projects, habits,
  style. Budget ~2000 chars.
- `MEMORY.md` — Jarvis's own operational notes: environment facts,
  conventions, quirks worth remembering. Budget ~3200 chars.
- `projects/<slug>.md` — optional per-project notes (goals, decisions,
  architecture); not auto-injected yet, but searchable.

Entry format inside a block: one `- ` bullet per fact, declarative phrasing,
absolute dates only ("prefers X", "on 2026-07-18 decided Y" — never "today"
or imperative "always do X"). Keep each file under its budget by merging or
dropping the least valuable entries.

## Operations

- **Remember** ("remember that …"): read the target block first, then add or
  update the fact. Durable facts about the user → `USER.md`; operational
  notes → `MEMORY.md`. Skip anything that will be stale within a week (task
  progress, one-off states) — say so instead of saving. Confirm in one short
  sentence.
- **Forget** ("forget that …", "forget what you know about …"): find the
  matching entries, remove them, confirm what was removed. If nothing
  matches, say so.
- **Recall** ("what do you remember about …"): read the blocks (and grep
  `vault/` if the blocks don't answer) and reply in 1–3 spoken-prose
  sentences. No markdown — this is read aloud.
