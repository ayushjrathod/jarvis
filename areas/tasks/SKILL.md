---
name: tasks
description: Personal task management in vault/tasks/
# Trigger words match as in-order subsequences ("mark task done" matches
# "mark the domain task as done"), so keep them compact.
triggers:
  - add task
  - new task
  - create task
  - remind me
  - complete task
  - mark task
  - finish task
  - task done
  - task complete
quick_triggers:
  - morning brief
  - daily brief
  - read brief
  - my brief
  - list tasks
  - my tasks
  - open tasks
  - what tasks
allowed_tools:
  - Read
  - Glob
  - Grep
  # Edit(path) rules govern Write/MultiEdit too; a "Write(path)" rule is NOT
  # recognized by the CLI and gets silently ignored (learned the hard way)
  - "Edit(vault/tasks/**)"
quick_allowed_tools:
  - Read
  - Glob
  - Grep
---
# Tasks area

You manage the user's tasks as markdown files in `vault/tasks/`, one file per
task. Never touch files outside `vault/tasks/` for task operations.

## Task file format

Filename: short kebab-case slug of the title, e.g. `renew-the-domain.md`
(add `-2` etc. on collision). Contents:

```markdown
---
title: Renew the domain
status: open          # open | done
created: YYYY-MM-DD   # today
due: YYYY-MM-DD       # only if the user gave one (else omit the key)
source: voice         # the task's source field
---
Optional free-text notes from the user's phrasing.
```

## Operations

- **Capture** ("add a task: …", "remind me to …"): create the file. Title =
  the essential action, cleaned up (not the raw utterance). Parse natural
  dates ("by Friday", "next week") into `due:` when unambiguous; otherwise
  omit `due`. Reply with one short confirmation sentence naming the title.
- **Complete** ("mark task X done", "finish the task about Y"): find the open
  task whose title/slug best matches; set `status: done` (keep the file).
  If nothing matches clearly, say so and list the closest open tasks.
- **List** ("list my tasks", "what are my open tasks"): read all files in
  `vault/tasks/` with `status: open`, reply as short spoken prose ordered by
  due date (undated last). No markdown bullets — this gets read aloud.
- **Brief** ("morning brief", "read my brief"): read today's
  `vault/briefs/YYYY-MM-DD.md`; if missing, the most recent file in
  `vault/briefs/`. Read it back condensed and speakable — plain prose, no
  markdown, keep the structure (today / tasks / notes / email).

Replies are spoken by TTS: 1–3 plain sentences unless listing.
