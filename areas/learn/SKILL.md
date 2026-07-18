---
name: learn
description: Author new area skills from workflows ("learn this as a skill")
triggers:
  - learn this
  - create a skill
  - save this as a skill
  - new skill
  - make a skill
allowed_tools:
  - Read
  - Glob
  - Grep
  # Edit(path) rules cover Write too; a "Write(path)" rule is silently ignored
  - "Edit(areas/**)"
---
# Learn area

<!-- Authoring standards adapted from hermes-agent's /learn command
     (MIT, references/hermes-agent agent/learn_prompt.py). -->

You distill a workflow, procedure, or set of instructions into ONE area
skill under `areas/`, in this system's format. Gather the source material
with your read tools (the user's request names it: a directory, a file, or
"what we just did"), then write the skill package.

## Area skill format

`areas/<name>/SKILL.md` with YAML frontmatter:

- `name`: lowercase-hyphenated, **class-level** — a category of recurring
  work ("expenses", "server-maintenance"), never one task's codename. If the
  name only fits today's task, it is wrong.
- `description`: one short sentence.
- `triggers`: spoken phrases that should route here, matched as in-order
  word subsequences — keep them compact and realistic ("check expenses"
  matches "check my expenses from May"). Optional `quick_triggers` for
  read-only questions answered aloud.
- `allowed_tools`: least privilege. Read-only (`Read`, `Glob`, `Grep`)
  unless the skill must write; scope writes with `Edit(path/**)` rules —
  a `Write(path)` rule is silently ignored by the CLI.

Body: markdown instructions injected as context when the area matches.
Sections in order: what the area does, file formats/conventions it owns,
**Operations** (one per trigger family, with exact steps), and a
**Verification** note (how a run knows it worked). 100–200 lines max;
support material goes in `references/`/`scripts/` inside the area dir with
a one-line pointer from SKILL.md.

## Hard rules

- Prefer PATCHING an existing area over creating a near-duplicate — check
  `areas/` first.
- Never invent flags, paths, or APIs: if you didn't see it in the source
  material or this conversation, don't write it.
- Replies are spoken: confirm in 1–2 plain sentences naming the skill.
