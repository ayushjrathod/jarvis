---
task_type: weekly-review
allowed_tools:
  - Read
  - Glob
  - Grep
  - "Edit(vault/briefs/**)"   # Edit(path) rules cover Write; Write(path) is ignored
---
Write a weekly review to `vault/briefs/week-{{DATE}}.md` covering
{{WEEK_START}} through {{DATE}} (today, {{DATE_HUMAN}}).

Read `vault/tasks/`, `vault/notes/`, and the week's daily briefs in
`vault/briefs/`, then write:

```markdown
# Weekly review — week ending {{DATE}}

## What happened
<3-5 sentences: tasks completed this week, themes from the notes/briefs>

## Open loops
<every open task, one line each, flagged if overdue or stalled (created >2
weeks ago, still open)>

## Suggested focus
<2-3 sentences for the coming week, grounded in the open loops>
```

Under 400 words, speakable plain prose inside each section. Overwrite if the
file exists. Do not modify any other file.
