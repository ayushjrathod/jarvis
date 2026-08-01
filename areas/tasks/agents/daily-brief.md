---
task_type: daily-brief
allowed_tools:
  - Read
  - Glob
  - Grep
  - "Edit(vault/briefs/**)"   # Edit(path) rules cover Write; Write(path) is ignored
---
Write today's daily brief to `vault/briefs/{{DATE}}.md`. Today is {{DATE_HUMAN}}.

Build it from:

1. **Open tasks** — every file in `vault/tasks/` with `status: open`. Order:
   overdue first (due < {{DATE}}), then due today, then upcoming, then undated.
2. **Recent notes** — files in `vault/notes/` modified in the last 3 days;
   one line each on what changed or matters.

The brief is deliberately email-free (user's call, 2026-08-01). A Gmail MCP
server IS connected in this config dir, so its tools appear in your tool list —
do NOT call them. They are not granted, and every attempt is a denial that
costs a turn and muddies the run's status.

Format (total under 300 words, written to be read aloud by TTS later —
plain declarative sentences, no links, no tables):

```markdown
# Brief — {{DATE}}

## Today
<2-3 sentences: the day's shape — what's overdue, what's due, what deserves focus>

## Open tasks
<one line per task: title, due date if any>

## Recent notes
<one line per recent note>
```

Overwrite the file if it already exists. Do not modify any other file.
