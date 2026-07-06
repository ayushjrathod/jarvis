---
task_type: daily-brief
allowed_tools:
  - Read
  - Glob
  - Grep
  - "Edit(vault/briefs/**)"   # Edit(path) rules cover Write; Write(path) is ignored
  - mcp__gmail
---
Write today's daily brief to `vault/briefs/{{DATE}}.md`. Today is {{DATE_HUMAN}}.

Build it from:

1. **Open tasks** — every file in `vault/tasks/` with `status: open`. Order:
   overdue first (due < {{DATE}}), then due today, then upcoming, then undated.
2. **Recent notes** — files in `vault/notes/` modified in the last 3 days;
   one line each on what changed or matters.
3. **Email** — ONLY if Gmail MCP tools are available to you: unread/important
   threads from the last 24h, one line each, no bodies. If no Gmail tools are
   available, omit this section entirely and silently.

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

## Email
<only if available>
```

Overwrite the file if it already exists. Do not modify any other file.
