# Mission Control v2 — Layered Vision Plan

_Written 2026-07-18 (session 11). Status: **planning complete — awaiting user
review before any Phase F code.** Full research reports in
`context/research/2026-07-18-{memory-repos,hermes-agent,openjarvis,khoj}.md`;
new clones (gitignored): `references/{hermes-agent,openjarvis,mem0,graphiti,letta,khoj}`._

The user's v2 vision is a 14-layer personal-AI architecture (brain, memory,
learning, skills, tools, planner, multi-agent, background workers, personal
OS, observability, safety, self-improvement). This doc maps each layer onto
the v1 system, summarizes what the reference repos teach, and lays out the
phased roadmap.

## Ground rules (carried over from v1 — not re-litigated)

- Single dispatch path: the dispatcher stays the ONLY Claude invoker; every
  new layer is either a dispatcher feature, a Claude Code skill, or a timer.
- Local-first, CPU-only, SQLite + markdown vault, systemd user units.
- Patterns lifted from references with attribution (khoj is **AGPL: patterns
  only, never code**; the rest are MIT/Apache-2.0); no new runtime deps
  without asking. Boring readable Python, one maintainer.
- Claude (subscription, headless CLI) is the only LLM. "Parallel model
  execution" therefore means tiered/graded Claude invocations, not local LLM
  serving — the research confirmed the local-worker paradigms don't transfer.

## Layer-by-layer gap analysis (v1 → v2)

| # | Layer | v1 already has | Gap for v2 | Served by phase |
|---|---|---|---|---|
| 1 | Brain | Dispatcher: quick/agentic classifier, streaming SSE→TTS, refusal + usage-limit fallback | Graded complexity tiers driving effort/budget | H |
| 2 | Memory | Markdown vault + SQLite runs/tasks; nightly backup | **Biggest gap.** Core blocks, episodic capture, semantic search, knowledge graph, retrieval at answer time | **F**, J |
| 3 | Learning | — | **Biggest new capability.** Closed loop: task → reflection → skill create/patch → reuse | **G** |
| 4 | Skills | areas/ = Claude Code skills (only `tasks`); zero-core-change plugin contract | Auto-generated skills, usage telemetry, lifecycle curation, /learn | G |
| 5 | Tools | Per-task-type allowedTools, budget caps, timeouts; MCP (Gmail pending) | Largely done; add read-before-write guard, per-tool logging via run_steps | G, H |
| 6 | Planner | Quick-vs-agentic classifier | Graded tiers (H); full goal decomposition **deferred** (see non-goals) | H (partial) |
| 7 | Multi-agent | Deferred in v1 by design | Still deferred; areas/ + per-area agent prompts is the future shape | — |
| 8 | Background workers | Timers: daily brief, weekly review, backup; queue dir; limit-aware requeue | Nightly consolidation, NL→standing automations, notify-or-not gate | F, I |
| 11 | Personal OS | Vault only; Gmail MCP stub | Ingest + index vault/exports/PDFs/images → everything searchable | I (spine in F) |
| 12 | Observability | SQLite run log, SSE agent monitor, expandable rows | TTFT/ITL stats, step timeline, success-rate aggregates, X-ray footer | **H** |
| 13 | Safety | allowedTools, spend caps, read-only scopes, local-only | Cross-cutting: reflection write-guards, curator archive-not-delete, memory injection-scan idea | F–J |
| 14 | Self-improvement dashboard | — | "What did I learn / what failed / what should be a skill" view over G+H data | H |

(Layers 9–10 were absent from the vision text as provided.)

## What the research taught (condensed — full reports in context/research/)

**hermes-agent (MIT).** The famous learning loop is prompt-driven, no
training: (a) system-prompt nudges ("after 5+ tool calls, save a skill;
patch stale skills immediately"; memory = declarative facts only, 7-day
staleness test); (b) a background **review fork** of the conversation with
skill/memory tools only, triggered by tool-call counters, whose review
prompts encode the real wisdom — patch-before-create preference order,
class-level skill naming, anti-capture rules; (c) `/learn` user-directed
skill authoring with hardline standards (≤60-char descriptions, Verification
section); (d) a **curator** aging skills active→stale→archived from a usage
telemetry sidecar. Key mapping: their warm-cache fork ≡ our
`claude -p --resume` (already built, measured ~6× cheaper). DSPy/GEPA
"self-evolution" is research-only — skip.

**mem0 + graphiti + letta (all Apache-2.0).** Memory = four patterns that
all fit SQLite + vault: mem0's **additive extraction prompt**
(observation-date grounding, anti-hallucination integer ids) run nightly;
letta's **core memory blocks** (bounded, always-in-context, self-edited) and
**sleep-time consolidation agent**; graphiti's **bi-temporal knowledge graph**
(facts as embedded sentences with valid_at/invalid_at/expired_at; pure-Python
contradiction invalidation ~35 lines; BFS = recursive CTE — no graph DB);
hybrid retrieval fused with **RRF** (~15 lines). letta ships sqlite-vec on
SQLite in production — proof our stack suffices. CPU embeddings are solved
(~34MB bge-small int8 ONNX); FTS5-only is a legitimate first step.

**openjarvis (Apache-2.0, Stanford).** Headline features (local model zoo,
six hybrid local+cloud paradigms, energy research) **don't transfer** — they
presume GPU workers and per-token cloud cost. What transfers is the
observability layer, almost wholesale: TTFT + inter-token-latency
percentiles (~50 dep-free lines), a `trace_steps` timeline table (persist
the stream-json events our runner already parses and discards), success-rate
aggregates + `/stats`, the per-message **X-ray footer** UI, idempotent
additive `ALTER TABLE` migration loop, typed-error marker discipline, and a
graded complexity classifier mapping to token-budget tiers. Their
`ClaudeCodeAgent` validates our architecture — they wrap Claude Code too.

**khoj (AGPL — patterns only, re-implement).** The ingest spine for Layer
11: per-format processors normalizing everything to one Entry shape;
**heading-ancestry-prefixed chunks** (self-describing, with `file://…#line=`
URIs); **hash-diff incremental re-index** (mtime prefilter → per-chunk MD5
set-difference → embed only what changed; empty push = deletion tombstone);
index-time **date extraction** into a side table (makes "last week" queries
pure SQL); deterministic query filters (`dt>=`, `file:`, `+word`) applied
before vector scan; 256-token chunks, 0.18 cosine cutoff, gte-small-class
models proven adequate on CPU. Their automations validate our timers as the
better substrate but contribute two ideas: **NL → standing automation** (one
LLM call → cron row) and the **notify-or-not gate** (post-run judgment: is
this worth surfacing?). No calendar/email connectors exist in khoj.

## The v2 design in one paragraph

Every interaction already flows through the dispatcher, so v2 is mostly
*capture, consolidate, retrieve, reflect*: interactions append cheap raw
**episodes** to SQLite; a nightly **consolidation agent** (letta sleep-time
pattern, mem0 extraction prompt, graphiti edge invalidation) distills them
into bounded **core memory blocks** (`vault/memory/*.md`, injected into every
prompt) and a bi-temporal **fact/graph store**; an **ingest pipeline** (khoj
patterns) makes the vault + exports searchable via FTS5 (embeddings later);
a `/memory/search` endpoint serves voice, dashboard, and agents alike. On
top of that, agentic runs that exceed a tool-call threshold enqueue a cheap
**reflection fork** (`claude -p --resume` + Hermes review prompts, scoped to
`Edit(areas/**, vault/memory/**)`) that patches or creates skills — the
closed learning loop — governed by usage telemetry and a deterministic
curator. Observability rides on a `run_steps` timeline + latency stats +
`/stats` aggregates, surfaced as an X-ray footer and a self-improvement view.

## Phased roadmap

Each phase ends with acceptance tests + **STOP for user review** (same
protocol as v1). Order chosen so every phase delivers standalone value and
later phases consume earlier phases' data.

### Phase F — Memory foundation (Layer 2 core, Layer 8 nightly worker)
1. **Episodes capture**: `episodes` table (source, content, valid_at,
   session_id) appended by quick + agentic paths — no LLM cost.
2. **Core memory blocks**: `vault/memory/MEMORY.md` (~800-token budget) +
   `USER.md` (~500) + per-project blocks; injected into every quick/agentic
   prompt by the dispatcher (letta compile pattern, Hermes budgets).
3. **Vault ingest + FTS5**: khoj-pattern `dispatcher/ingest.py` — heading-
   ancestry chunks, MD5 hash-diff incremental re-index, `entries` +
   FTS5 mirror + `entry_dates`; timer-driven. Zero new deps.
4. **`/memory/search`**: FTS5 BM25 + date/file/word pre-filters; RRF-ready.
5. **Nightly consolidation agent**: `mission-memory-consolidate` timer →
   headless run over the day's episodes with mem0's additive-extraction
   prompt + letta's sleep-time discipline, editing block files under
   `Edit(vault/memory/**)` scope; append-only `memory_history` audit table.
   Acceptance: asking Jarvis something told to it days earlier works.

### Phase G — Learning loop + skill lifecycle (Layers 3+4)
1. **Prompt nudges**: Hermes SKILLS/MEMORY guidance into runner system
   prompt (declarative memories, save-a-skill-after-complex-task).
2. **Reflection fork**: count tool_use events per run; ≥ threshold →
   low-priority queued `claude -p --resume <session_id>` with adapted Hermes
   review prompts, allowedTools `Read Grep Glob Edit(areas/**)
   Edit(vault/memory/**)`, small budget cap; read-before-write guard;
   `reflections` logged + SSE event ("what Jarvis learned" in dashboard).
3. **Skill usage telemetry**: `skill_usage` table bumped by the area matcher
   + PostToolUse hooks on `areas/**/SKILL.md` reads/edits.
4. **`/learn`**: dispatcher endpoint + "jarvis, learn this as a skill" voice
   trigger; Hermes authoring standards adapted to Claude Code SKILL.md.
5. **Curator (deterministic only)**: monthly timer, active→stale→archived
   lifecycle over telemetry, `git mv` to `areas/.archive/` (git = rollback);
   LLM consolidation pass deferred until skill count justifies it.

### Phase H — Observability + graded brain (Layers 12, 1, 6-partial, 14)
1. **run_steps timeline**: persist runner stream-json events
   `(run_id, step_index, step_type, ts, duration, payload)` — also unblocks
   the session-3 deferred "stream agentic runs to dashboard" item; scope together.
2. **Latency stats**: TTFT + ITL percentiles + tok/s on the quick path
   (openjarvis itl.py pattern); additive `runs` columns via the idempotent
   migration-loop pattern.
3. **`/stats` + dashboard**: success rate, cost by source, top skills
   (Hermes insights.py shape); X-ray footer line on every interaction row.
4. **Graded classifier**: complexity tiers → `--effort`/budget per task.
5. **Self-improvement view** (Layer 14): weekly-review section + dashboard
   panel answering "what did I learn / what failed / what repeats enough to
   become a skill" — SQL over reflections + run_steps + skill_usage.

### Phase I — Personal OS + automations (Layers 11, 8)
1. **Ingest expansion**: exports dir (`vault/inbox/` convention) — PDFs,
   plaintext, bookmarks, statements; per-format processors, khoj-style.
   OCR for images/scans behind dep approval (rapidocr-onnxruntime, pymupdf).
2. **NL → standing automations**: `automations` SQLite table + one generic
   `mission-automations.timer` polling due rows into the existing queue;
   voice: "every morning, tell me X". Mechanical cron sanitizing.
3. **Notify-or-not gate**: post-run quick-path judgment before speaking/
   surfacing timer results; suppressed results logged only.
4. Gmail MCP (user OAuth pending) + calendar export land here as sources.

### Phase J — Semantic + graph memory (Layer 2 deep)
1. **Embeddings**: sqlite-vec + bge-small-en-v1.5 int8 ONNX (~34MB) warm in
   the dispatcher — **needs dep approval**; hybrid search becomes
   FTS5 + cosine fused by RRF; 0.18 distance cutoff starting point.
2. **Knowledge graph**: `entities`/`edges`/`mentions` tables (graphiti
   schema), facts-as-sentences, bi-temporal invalidation in consolidation,
   1-hop graph expansion in `/memory/search` (recursive CTE).
3. **Weekly reconciliation**: mem0 ADD/UPDATE/DELETE pass over memories;
   later: label-propagation communities → `vault/memory/topics.md`.

## Dependency gates (ask user, per working style)

| Dep | Phase | Purpose | Fallback if declined |
|---|---|---|---|
| `sqlite-vec` | J | vector search in mission.db | FTS5-only recall |
| `onnxruntime`/`fastembed` (+bge-small) | J | local CPU embeddings | FTS5-only |
| `rapidocr-onnxruntime`, `pymupdf` | I | image/PDF ingestion | text formats only |
| `spacy` (en_core_web_sm) | J (optional) | entity boosts in retrieval | Claude tags entities in consolidation |

Phases F, G, H need **zero new dependencies**.

## Explicit non-goals (v2)

- Multi-agent fan-out / CEO-agent hierarchies (Layer 7) — still deferred;
  the areas/ layout is the seam where it would land later.
- Full planner with goal decomposition + dependency graphs (Layer 6) —
  Claude Code's own agentic loop already plans within a run; revisit only if
  multi-run workflows appear.
- DSPy/GEPA prompt evolution, trained routers, local LLM workers, hybrid
  local+cloud paradigms, cross-encoder reranking, energy telemetry beyond
  curiosity, second servers of any kind (Django/Postgres/APScheduler).
- Skill marketplace/sharing (Layer 4 wishlist) — one maintainer, no market.

## Decisions needed from the user before Phase F

1. Approve the phase order F→G→H→I→J (or reprioritize — e.g. H before G if
   observability feels more urgent than learning).
2. Episode capture scope: log **all** voice/dashboard interactions to the
   episodes table by default, or opt-in per source? (Privacy of always-on
   capture is a user call.)
3. Dependency gates above — none needed until Phase I/J, decide then.
4. CLAUDE.md phase tracker + locked-decisions updates land after approval.
