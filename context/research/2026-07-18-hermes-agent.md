# Hermes-Agent Reference Audit (for Mission Control v2)

_Research subagent report, 2026-07-18 (v2 planning). Clone (gitignored) at
`references/hermes-agent`, HEAD `e5afc0d` (2026-07-18)._

## License

**MIT** (`LICENSE`, Copyright 2025 Nous Research) — we may lift code and prompt text freely with attribution comments. One caveat: the `darwinian-evolver` optional skill wraps an **AGPL-3.0 upstream** (imbue-ai/darwinian_evolver) — never lift anything from that upstream; the skill itself only shells out to it.

---

## 1. The learning loop (Layer 3) — exactly how it works

There is **no monolithic state machine**; it is four cooperating mechanisms, all prompt-driven (no fine-tuning, no DSPy):

### (a) Always-on system-prompt nudge — the source of the "5+ tool calls" claim
`agent/prompt_builder.py:181`, constant `SKILLS_GUIDANCE`, injected whenever `skill_manage` is in the toolset (`agent/system_prompt.py:226`). Verbatim:

> "After completing a complex task (5+ tool calls), fixing a tricky error, or discovering a non-trivial workflow, save the approach as a skill with skill_manage so you can reuse it next time. When using a skill and finding it outdated, incomplete, or wrong, patch it immediately with skill_manage(action='patch') — don't wait to be asked. Skills that aren't maintained become liabilities."

So "5+ tool calls" is an *instruction to the model*, not a counter. The companion `MEMORY_GUIDANCE` (same file, ~line 153) draws the memory/skill boundary: durable declarative facts → memory; procedures → skills; nothing that will be stale in 7 days; **declarative phrasing, never imperative** ("'User prefers concise responses' ✓ — 'Always respond concisely' ✗" — imperative memories get re-read as directives later).

### (b) The mechanical closed loop: background review fork
This is the actual Hermes-style task→reflection→skill machinery.

- **Trigger (skills)**: `agent/turn_finalizer.py:493-499` — a cross-turn counter `_iters_since_skill` accumulates tool-calling iterations; when it reaches `skills.creation_nudge_interval` (**default 15**, `cli-config.yaml.example:683`) and `skill_manage` is available, `_should_review_skills = True`, counter resets.
- **Trigger (memory)**: `agent/turn_context.py:344-350` — `_turns_since_memory` counts user turns; fires at `memory.nudge_interval` (**default 10**, `cli-config.yaml.example:575`).
- **Execution**: `agent/turn_finalizer.py:511` — only after the final response is delivered and not interrupted, spawn a **daemon thread** (`agent/background_review.py`, `spawn_background_review_thread`) that forks the agent on a snapshot of the conversation with a **tool whitelist of memory + skill tools only**. Same model by default → full transcript replay hits the warm prompt cache (cheap); if routed to a cheaper aux model (`auxiliary.background_review.{provider,model}`), it replays a compact digest instead (`_digest_history`, keeps last 24 messages verbatim, collapses the rest).
- **Prompts** (`agent/background_review.py:170/181/286`): `_MEMORY_REVIEW_PROMPT`, `_SKILL_REVIEW_PROMPT`, `_COMBINED_REVIEW_PROMPT` (both triggers fired). The skill prompt is the crown jewel; key content:
  - "Be ACTIVE — most sessions produce at least one skill update... A pass that does nothing is a missed learning opportunity."
  - **Signals** (any one warrants action): user corrected style/tone/format ("frustration signals are FIRST-CLASS skill signals, not just memory signals"); user corrected workflow; non-trivial technique/fix/workaround emerged; a skill consulted this session was wrong/outdated → "Patch it NOW."
  - **Preference order** (pick earliest that fits): 1) patch a skill that was *loaded this session*; 2) patch an existing class-level "umbrella" skill; 3) add a support file (`references/<topic>.md` for session detail/knowledge banks, `templates/` for starter files, `scripts/` for re-runnable actions) + a one-line pointer in SKILL.md; 4) only then create a NEW skill, and the name MUST be class-level — "NOT a PR number, error string, codename... If the name only fits today's task, it's wrong."
  - **Anti-capture rules** (do NOT save): environment-dependent failures ("command not found", missing creds — the user can fix these); negative claims about tools ("X is broken" — "harden into refusals the agent cites against itself for months"); transient errors that resolved (the lesson is the retry pattern); one-off task narratives. If a tool failed on setup state, capture the FIX, never "this tool doesn't work".
  - "'Nothing to save.' is a real option but should NOT be the default."
  - The memory prompt is short: did the user reveal persona/preferences/expectations? Save with the memory tool, else "Nothing to save."
- **Safety invariant**: the review fork may only patch skill files it actually **read via skill_view during the review run** — read-before-write guard in `tools/skill_manager_tool.py` (`mark_background_review_skill_read`, `_background_review_read_before_write_guard`). Prevents patching content merely inferred from the transcript.
- Results are summarized back to the user (`summarize_background_review_actions`, modes off/on/verbose).

### (c) `/learn` — user-directed distillation
`agent/learn_prompt.py` (`build_learn_prompt`): one prompt telling the live agent to gather the named sources (dirs, URLs, "what we just did", pasted notes) with its existing tools and author ONE SKILL.md via `skill_manage`, with embedded HARDLINE authoring standards (`_AUTHORING_STANDARDS`): description **≤60 chars** ("NOT cosmetic: the system-prompt skill index truncates the description to 60 chars... anything past char 60 is silently cut and never routes. COUNT the characters"), fixed section order (When to Use / Prerequisites / How to Run / Quick Reference / Procedure / Pitfalls / **Verification**), frame actions through named agent tools, "NEVER invent flags, paths, or APIs — if you didn't see it in the source, don't write it", ~100–200 lines, scripts to `scripts/` not inlined. Empty request defaults to "the workflow we just went through in this conversation."

### (d) Curator — skill patching/curation at scale
`agent/curator.py`. Trigger: `maybe_run_curator()` (line 1998; called from `cli.py:12794` and `gateway/run.py`) — inactivity-based, no cron: runs when last run > `curator.interval_hours` (**default 7 days**) AND agent idle ≥ `min_idle_hours` (**2h**). Two passes:

1. **Deterministic lifecycle walk** (`apply_automatic_transitions`, line 305) over the telemetry sidecar: `active` → `stale` (no use/view/patch for `stale_after_days`=30) → `archived` (moved to `.archive/`, after 90d); reactivation on renewed use; **exempt**: pinned skills, cron-referenced skills, protected builtins, and never-used skills younger than the stale window ("use=0 is absence of evidence, not evidence of staleness").
2. **Optional LLM consolidation pass — OFF by default** (`DEFAULT_CONSOLIDATE = False`). `CURATOR_REVIEW_PROMPT` (line 417): "UMBRELLA-BUILDING consolidation pass, not a passive audit... hundreds of narrow skills where each one captures one session's specific bug is a FAILURE of the library." Hard rules: never delete (archive is max, recoverable); never touch bundled/external/pinned/protected/cron-referenced skills; never prune on use_count alone. Method: find prefix clusters, ask "would a human maintainer write this as N skills or one skill with N subsections?", consolidate via merge-into-umbrella / create-umbrella / demote-to-references, preserve package integrity (don't orphan a skill's `scripts/` when flattening). Output: human summary + **required fenced YAML block** (`consolidations: [{from, into, reason}]`, `prunings: [{name, reason}]`); every archived skill must appear in exactly one list; `absorbed_into=` on delete drives automatic rewriting of cron-job skill references. A **dry-run banner** (`CURATOR_DRY_RUN_BANNER`) turns it into report-only mode. `_classify_removed_skills` (line ~620) cross-checks the LLM's self-report against the actual tool calls.
   - **Rollback**: `agent/curator_backup.py` — pre-run tar.gz snapshot of the whole skills tree (incl. `.usage.json`, `.archive/`, cron jobs.json), keep 5, rollback itself is undoable. Per-run reports at `logs/curator/<ts>/run.json + REPORT.md`.

**Telemetry underpinning it all**: `tools/skill_usage.py` — sidecar `~/.hermes/skills/.usage.json` (deliberately *not* frontmatter, to keep telemetry out of authored content): per-skill `use_count/view_count/patch_count`, `last_used_at/last_viewed_at/last_patched_at`, `created_at`, provenance flag (agent-created vs bundled/hub), `state`, `pinned`. Bumped best-effort by `skill_view`/`skill_manage`; atomic writes + file lock; a broken sidecar never breaks a tool call.

---

## 2. Skill format & retrieval (Layer 4)

**Package**: `<name>/SKILL.md` + optional `references/`, `templates/`, `scripts/`, `assets/` (identical philosophy to Claude Code skills). Frontmatter (`AGENTS.md` §Skills, ~line 870): `name` (lowercase-hyphenated), `description` (≤60 chars, one sentence), `version: 0.1.0`, `author`, `license`, `platforms` (OS gating list), `metadata.hermes.{tags, category, related_skills, config}`, plus conditional-activation keys (`requires_tools/toolsets`, `fallback_for_tools` — hide the skill when the primary tool it substitutes for is available; `agent/prompt_builder.py:_skill_should_show`).

**Versioning**: `version:` is a write-once convention — **zero mechanical version handling** (`grep -c version tools/skill_manager_tool.py` → 0). Real "versioning" is the curator snapshot/rollback system plus archive-not-delete. Bundled skills get pytest tests (`tests/skills/`); **agent-created skills have no automated tests** — quality is enforced by the `## Verification` body section, the background-review patch loop, and the curator.

**Mutation surface**: `tools/skill_manager_tool.py` — actions `create / edit / patch (find-and-replace) / delete (→archive, with absorbed_into) / write_file / remove_file`, with frontmatter validation, name/category validation, size limits, pinned-guard, and the read-before-write guard.

**Retrieval/injection — no embeddings, no vector store**: `build_skills_system_prompt` (`agent/prompt_builder.py:1459`) renders the ENTIRE index into the system prompt — categories with one `name: description` line per skill (this is why 60 chars is hardline) — headed by a "## Skills (mandatory)" instruction: scan before replying, load anything "even partially relevant" via `skill_view`, "err on the side of loading", "load them even for tasks you already know how to do, because the skill defines how it should be done here", and "If a skill has issues, fix it with skill_manage(action='patch')." The model does the routing. Performance: two-layer cache (in-process LRU + on-disk `.skills_prompt_snapshot.json` validated by an mtime/size manifest). Under coding focus, non-coding categories are demoted to a names-only line but **never removed**: "agent-created skills are the model's project memory, and models don't reach for skills_list to rediscover what the index stops showing them." Skill slash commands are injected as a **user message, not system prompt**, to preserve prompt caching (`agent/skill_commands.py`, `AGENTS.md:381`).

---

## 3. Memory (Layer 2)

**Built-in store** (`tools/memory_tool.py`): two bounded markdown files under `~/.hermes/memories/` — `MEMORY.md` (agent notes: environment facts, conventions, tool quirks; **2200 chars ≈ 800 tokens**) and `USER.md` (user profile: preferences, style, expectations; **1375 chars ≈ 500 tokens**). Entries delimited by `§`, may be multiline. One `memory` tool with `add / replace / remove` actions; replace/remove match by **short unique substring**, not IDs. Char limits (not tokens) because they're model-independent. Both files are injected into the system prompt as a **frozen snapshot at session start**; mid-session writes hit disk immediately but do NOT touch the system prompt — preserves the prompt-cache prefix for the whole session. Entries are scanned for injection/exfiltration patterns before entering the prompt (`tools/threat_patterns.py`, strict scope — poisoned memory persists across sessions, so it gets the broadest pattern set).

**What gets remembered**: governed entirely by `MEMORY_GUIDANCE` (see §1a) — the notable rules are the 7-day staleness test, the ban on task-progress/PR-number/commit-SHA entries, and declarative-not-imperative phrasing.

**Episodic memory** = full transcripts in SQLite + FTS: `hermes_state.py` (SessionDB: `sessions`, `messages`, `session_model_usage` tables) + `tools/session_search_tool.py` — FTS5 discovery (query → top-N sessions with snippet, ±5-message window, first/last-3-message "bookends"), scroll (anchored ±window), browse (recent). Zero LLM cost. Two ranking lessons worth stealing: subagent/tool sessions hidden entirely; **cron sessions demoted (not excluded)** because scheduled jobs' repetitive vocabulary dominates BM25 and causes "recall blindness" (#19434).

**Extraction/consolidation points**: the nudged background review (§1b); plus a **memory flush at session boundaries** — before `/new`, `/reset`, exit, or context compression, if the session had ≥ `flush_min_turns` (6) user turns, the agent gets a chance to save memories before context is lost; run async so `/new` stays responsive (`cli.py:7006` `_launch_session_boundary_memory_flush`).

**Provider plugin system**: `agent/memory_provider.py` ABC orchestrated by `agent/memory_manager.py` with a **one-external-provider limit** ("prevents tool schema bloat and conflicting memory backends"). Plugins under `plugins/memory/`: mem0, honcho, hindsight, byterover, supermemory, retaindb, openviking, holographic. `plugins/memory/query_rewrite.py`: an aux-model rewrites the latest user message into ONE retrieval question, with injection-hardening regexes on both input and output.

There is **no knowledge graph in the memory path** — `agent/learning_graph.py` is a *visualization* (desktop "learning made visible" graph), not a retrieval structure.

---

## 4. Self-evolution / DSPy / GEPA — verdict: research-only, nothing to lift

DSPy and GEPA appear **only as optional skills**: `optional-skills/mlops/research/dspy/SKILL.md` (teaches the agent to use the DSPy library on user projects) and `optional-skills/research/darwinian-evolver/SKILL.md` (drives Imbue's AGPL evolution loop via subprocess; needs API keys, a fitness function, 50–500 LLM calls per run). Neither is wired into Hermes' own improvement loop. Hermes' actual "self-evolution" **is** §1's prompt-driven review + curator. For our closed loop: copy the prompts and lifecycle, ignore DSPy/GEPA entirely (also blocked for us by: no API key, CPU-only, eval-harness burden).

---

## 5. Architecture worth noting

- **Gateway/multi-surface**: one process, platform adapters over a shared session layer (`gateway/session.py`) with reset policy (idle/daily/none) and an auto-continue freshness window. Structurally identical to our dispatcher+clients; we already lifted the reset-policy/continuity ideas from OpenClaw (sessions 9–10), so little new here.
- The key structural difference from us: **they persist full message transcripts to SQLite with FTS**, which is what makes `session_search`, `insights`, and the review fork's transcript replay possible. Our runs table stores task-level rows only — this is the enabling investment for Layer 2.
- `agent/insights.py`: pure-SQL usage analytics (tokens/cost/tools/**skills used**/activity patterns) — trivially portable to our SQLite for a dashboard widget.
- Aux-model task routing (`auxiliary.*` config keys per background task) — conceptually our `models.fallback`/`models.quick` config, generalized.
- Kanban SQLite task board with heartbeats/block/complete handoffs (`KANBAN_GUIDANCE`) — multi-agent orchestration; out of scope (our spec defers multi-agent fan-out).

---

## 6. Liftable patterns, ranked by value

Our targets: `dispatcher/`, `areas/<name>/SKILL.md`, `vault/`. One mapping insight up front: **their "fork the agent with warm cache + tool whitelist" maps exactly onto our `claude -p --resume <session_id> --allowedTools ...`** — which we already built and measured at ~6× cheaper via prompt cache (session 10). The reflection loop is almost free plumbing for us.

| # | Pattern | Source | Maps onto us | Effort | Layer |
|---|---|---|---|---|---|
| 1 | **Post-task reflection fork** (the closed loop) | `agent/background_review.py`, `agent/turn_finalizer.py:493-527`, `agent/turn_context.py:344` | Dispatcher already parses stream-json: count tool_use events per agentic run (and per quick-path session). When count ≥ config threshold (`reflection_min_tool_calls`), enqueue a **low-priority reflection task** = `claude -p --resume <session_id>` with the adapted review prompt, `--allowedTools "Read Grep Glob Edit(areas/**) Edit(vault/memory/**)"`, small budget cap. Runs after the answer is delivered (queue), never blocking the user — same invariant they enforce. Log the reflection's actions to a `reflections` table / SSE event so the dashboard shows "what Jarvis learned". | **M** | **3** |
| 2 | **The three review prompts** (verbatim lift + adapt) | `agent/background_review.py:170-380` | The signals list, the 4-step preference order (patch loaded skill > patch umbrella > add support file > new class-level skill), the anti-capture rules (env failures, negative tool claims, transients, one-off narratives), and "Nothing to save is not the default" are the distilled operational wisdom of this repo. Adapt tool names to Claude Code (Edit/Write on `areas/**/SKILL.md`). | **S** | **3** |
| 3 | **SKILLS_GUIDANCE + MEMORY_GUIDANCE nudges** | `agent/prompt_builder.py:153-190` | Append to the agentic runner's system prompt (`--append-system-prompt`) and/or root CLAUDE.md for dispatcher-spawned runs: the "5+ tool calls → save a skill", "patch outdated skills immediately", the memory 7-day-staleness test, and declarative-not-imperative memory phrasing. | **S** | **3** |
| 4 | **Two-store bounded memory: MEMORY.md + USER.md** | `tools/memory_tool.py` (design + limits), `MEMORY_GUIDANCE` | `vault/memory/MEMORY.md` (agent notes, ~800-token budget) + `vault/memory/USER.md` (user profile, ~500-token budget), `§`-delimited entries; dispatcher injects both into every quick and agentic prompt (fits locked decision #4). Enforce char budget in the write path. The frozen-snapshot/prompt-cache trick matters less for us (per-invocation prompts), skip that part. | **S–M** | **2** |
| 5 | **Skill usage telemetry + lifecycle states** | `tools/skill_usage.py`, `agent/curator.py:305` | SQLite table `skill_usage(name, use_count, view_count, patch_count, last_*_at, created_at, created_by, state, pinned)` in `data/`. Bumps: dispatcher's area-trigger matcher on dispatch; a PostToolUse hook observing `Read` of `areas/**/SKILL.md` for in-run loads; `Edit` of same for patches. States active/stale/archived + pinned exactly as theirs, incl. the never-used grace floor and timer-referenced exemption. | **S** | **4** |
| 6 | **Curator** — deterministic pass now, LLM pass later | `agent/curator.py` (both passes), `agent/curator_backup.py` | Deterministic lifecycle walk = boring Python over table #5, run from a monthly systemd user timer; archive = `git mv` into `areas/.archive/` — **git replaces their tarball snapshot/rollback machinery for free** (commit before/after the pass). The LLM consolidation pass (CURATOR_REVIEW_PROMPT + dry-run banner + structured YAML output) becomes a queued agentic task — but they ship it **off by default** and its prompt is tuned for hundreds of skills; defer the LLM pass until skill count justifies it. | **S** (deterministic) / **M** (LLM pass, deferred) | **4** |
| 7 | **Session transcript FTS ("session_search")** | `tools/session_search_tool.py`, `hermes_state.py` schema | Persist transcripts (either log quick/agentic messages into a `messages` table, or ingest Claude CLI session JSONL from `~/.claude-per/projects/...` — we already know the session_id per run) + SQLite FTS5 + a `/search` endpoint / skill so briefs and voice queries can recall past work. Steal the ranking lesson verbatim: **demote timer-source sessions** or daily-brief output will dominate recall. This is the enabling investment for real episodic memory. | **M** | **2** |
| 8 | **`/learn` endpoint + authoring standards** | `agent/learn_prompt.py` | `POST /learn {request}` on the dispatcher → agentic task with a build_learn_prompt-style instruction, authoring standards rewritten for Claude Code SKILL.md format (name + description frontmatter, When to Use triggers matching our `dispatcher/areas.py` subsequence matcher, Verification section). Voice trigger: "jarvis, learn this as a skill" (continuity via `--resume` means "what we just did" works). | **S** | **4** |
| 9 | **Read-before-write guard for reflection** | `tools/skill_manager_tool.py:297-420` | Their invariant: the review fork may not patch a SKILL.md it hasn't read this run. For us: one paragraph in the reflection prompt + optionally a PreToolUse hook denying `Edit` on a SKILL.md with no prior `Read` in the session. Cheap insurance against hallucinated patches. | **S** | **3** |
| 10 | **insights SQL report** | `agent/insights.py` | Pure-SQL breakdowns over our runs (+future messages/skill_usage) tables → dashboard widget / weekly-review section ("top skills", cost by source, activity). Nice-to-have. | **S** | 2/4 adjacent |

**Priority read of the table**: items 1–3 constitute the Hermes-style closed loop (Layer 3); 4+7 are Layer 2; 5+6+8 are Layer 4 skill versioning/curation. Items 2, 3 are pure prompt-text lifts and should land first regardless of sequencing.

## 7. Explicit skips

- **DSPy / GEPA / darwinian-evolver** — optional-skill docs around external libraries, not the actual loop; needs API keys + eval harnesses; evolver upstream is AGPL.
- **External memory providers (mem0/honcho/hindsight/supermemory/...)** — cloud services or heavy deps; violates CPU-only/local-first; files + FTS5 cover v2.
- **MemoryProvider ABC layer** — with exactly one built-in store, an ABC now is speculative generality.
- **Kanban multi-agent board + delegation** — multi-agent fan-out is explicitly deferred in our spec.
- **Gateway platform adapters / relay / pairing / reset machinery** — our clients exist; OpenClaw already gave us continuity + reset-policy.
- **Curator LLM consolidation as default** — Hermes ships it off; wrong scale for a single-digit skill library.
- **skills_guard / skills_ast_audit security scanners** — built for third-party skill installs; we author our own.
- **Frozen-snapshot prompt-cache mechanics** — per-invocation `claude -p` prompts make it moot.
- **Aux-model routing framework / toolsets registry / trajectory compressor / MoA / ACP** — multi-provider machinery we don't have and don't want.
- **query_rewrite aux-model step** — an extra LLM call per query for retrieval phrasing; FTS5 doesn't need it at our scale.

**Config defaults worth copying as starting values**: skill reflection every 15 tool iterations (prompt says 5+), memory review every 10 turns, memory flush at ≥6 turns, memory 800/user 500 token budgets, curator weekly + 2h idle, stale 30d, archive 90d, snapshots via git.
