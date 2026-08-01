# Jarvis notes

<!-- Jarvis's own operational memory: environment facts, conventions, quirks.
     One "- " bullet per fact, declarative, absolute dates. Budget: 3200
     chars (enforced at prompt build). Edit freely by hand. -->

- The daily-brief timer's Write-tool permission gate (blocked unattended writes 2026-07-21 through 2026-07-23) no longer reproduced on 2026-07-24 — the brief wrote successfully via the Write tool, so treat as resolved unless it recurs.
- Gmail MCP tools are listed as available to the daily-brief agentic run but aren't pre-approved, so calling them hits an unattended permission gate that can't be granted interactively (seen 2026-07-23, 2026-07-24, 2026-07-26, 2026-07-28, and 2026-07-31 — a recurring, not one-off, condition); the brief correctly omits the Email section silently per its own instructions when this happens.
