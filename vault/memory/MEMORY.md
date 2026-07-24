# Jarvis notes

<!-- Jarvis's own operational memory: environment facts, conventions, quirks.
     One "- " bullet per fact, declarative, absolute dates. Budget: 3200
     chars (enforced at prompt build). Edit freely by hand. -->

- The daily-brief timer's agentic run (area:tasks) hits a Write-tool permission gate requiring interactive approval, blocking ALL unattended Write calls in that session (not just `vault/briefs/` — confirmed 2026-07-23 it also blocked editing memory files); recurred three nights running (first seen 2026-07-21, then 2026-07-22, 2026-07-23), still unresolved — needs a permissions setting granting Write for unattended/automation runs, or brief generation stays blocked every night until fixed.
