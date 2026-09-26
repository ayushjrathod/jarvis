# Mission Control — Personal Agentic Dashboard + Jarvis Voice Assistant

Local-first personal "life OS" on Arch Linux. A React dashboard and a fully local
voice assistant ("Jarvis") are two front-ends to the same brain: a **dispatcher**
service that owns every Claude invocation. Life areas are Claude Code skills,
memory is a markdown vault + SQLite, background agents run on systemd user timers.

> Note: this directory is named `jarvis/` but it is the **mission-control monorepo
> root**. The voice pipeline lives in the `jarvis/` subdirectory.

## Session protocol (maintain these files)

Every working session MUST:
1. **Read `context/STATE.md` first** — current phase, next action, open questions.
2. At session end: **update `context/STATE.md`** and **append a session log** to
   `context/sessions/YYYY-MM-DD-<n>.md` (decisions made, files touched, what's next).
3. Keep the **Phase tracker** below current.
4. **Keep STATE.md to roughly the last five sessions.** Step 1 reads it in full
   every single session, so it is the one file whose length is a recurring
   cost — it had reached 80KB before the 2026-08-11 trim. Roll older blocks
   verbatim into `context/state-archive.md` (never condense: the detail is the
   value) and leave the standing sections — Current phase, What runs where,
   What the user still needs to do, Known caveats, Deferred — at the foot,
   **corrected rather than appended to**. A stale standing section is worse
   than a long one: the pre-trim copy still said "31 unit tests" and called
   `mission-jarvis` "deliberately not enabled" while it was running.

## Phase tracker

| Phase | Scope | Status |
|---|---|---|
| A | Dispatcher core (API, queue watcher, classifier, headless runner, refusal fallback, SQLite logging, hooks) | **implemented, acceptance passing — awaiting user review** |
| B | Jarvis voice pipeline on the dispatcher | **done — user-verified on hardware 2026-07-06** |
| C | Tasks/work vertical slice + daily brief + weekly review | **implemented, acceptance passing** — the brief is deliberately **email-free** (user's call 2026-08-01); Gmail MCP is connected but not granted |
| D | React dashboard | **implemented — served by dispatcher at :8765, headless-render verified** |
| E | systemd wrap-up | **implemented — units live, auto-restart verified; reboot test = user** |
| F | v2 memory foundation (episode capture, core blocks, vault FTS index, /memory API, nightly consolidation) | **implemented, acceptance passing** (user waived review) |
| G | v2 learning loop (reflection fork on complex runs, skill telemetry, /learn + learn area, deterministic curator) | **implemented, acceptance passing** (user waived review) |
| H | v2 observability (run-step timeline + live SSE steps, TTFT/ITL latency, /stats, dashboard X-ray + stats widget) | **implemented, acceptance passing — awaiting user review** |
| I | v2 personal OS (txt/html/pdf/image ingest + vault/inbox, NL→standing automations, notify-or-not gate) | **implemented incl. PDF/OCR, acceptance passing** (review waived) |
| J | v2 semantic + graph memory (sqlite-vec hybrid search, bge-small local embeddings, bi-temporal fact graph, weekly reconcile) | **implemented, acceptance passing** (review waived) |
| — | Ask-about-my-screen (`<Super><Alt>a` → portal shot → popup → streamed answer) | **implemented, smoke passing — user E2E pending** (run scripts/setup_ask_screen.sh) |
| — | Spotify media control ("hey jarvis, play …" → MPRIS, deterministic) | **implemented — user E2E pending** (run scripts/setup_spotify.sh) |
| — | Phone access via Tailscale + installable PWA dashboard | **implemented, live — user add-to-home-screen pending** |
| K1 | Desktop control (computer-use T1: volume/mute/lock/launch/open/clipboard + allow-confirm-deny safety plane) | **implemented, live-verified** — `launch`/`open` were broken until 2026-07-27 (no display in the service env); brightness still deferred (needs deps, see notes) |
| K2 | Computer-use T2 (windows over AT-SPI) + T3 (browser tabs over DevTools HTTP + stdlib WS), zero new deps | **implemented, live-verified 2026-09-08..11** — list/focus + tabs/switch/read; window close, click-by-name and page actions deliberately not built (unverified verbs stay out) |
| — | Meta-task cost fix (`models.meta` → haiku for gate/parse tasks; notify gate no longer resumes) | **implemented, live-verified** — $0.141 → $0.0405 per gate run |
| — | Degraded mode (plan-cap failures answered extractively from the local hybrid index) | **implemented, live-verified** |
| — | Inbox watcher (first *event*-driven trigger: file lands in `vault/inbox/` → indexed + announced) | **implemented, live-verified** |
| — | Messages API backend | **built, then REMOVED 2026-07-27** — user is not funding an API key; subscription CLI only (recoverable at `ce93ea0`) |
| K2 | Computer-use T2 (AT-SPI) / T3 (browser over CDP) | **not started** — T3 needs no new deps; offered and deferred twice |

_Test count as of 2026-08-13: **738**, `.venv/bin/python -m unittest discover tests`.
Acceptance: `scripts/smoke_phase_a.sh` 8/8._

> **Open, unremediated: the 2026-08-13 full review** — 81 findings across all
> eight subsystems, **none fixed**. It disproved several claims made as fact in
> the operational notes below; those are corrected inline, but treat the rest as
> hypotheses until re-checked. Start with the vault/public-repo decision and the
> `Edit(**)` sanitizer hole.
>
> The report itself is **deliberately not in git**: it details a live escalation
> path and this repo is public. It sits uncommitted (and gitignored) at
> `context/reviews/2026-08-13-full-codebase-review.md`. Commit it once the repo
> is private or the finding is fixed — until then it exists only on this box, so
> do not assume a fresh clone has it.

v2 (phases F–J: memory, learning loop, observability, personal OS, graph) is
planned in `context/v2-plan.md` (user approved the direction 2026-07-18);
research audits behind it live in `context/research/`. **All five phases are
now implemented** — deps sqlite-vec/fastembed/pymupdf/rapidocr/jeepney were
user-approved and installed 2026-07-19.

**STOP for user review at the end of each phase.** Before Phase A code: API schema,
SQLite schema, and the four voice ABC signatures must be approved by the user.

## Hard constraints

- Arch Linux, Wayland, compositor-agnostic. **CPU-only** for all local models.
- One Python venv for backend/voice; Node for the React UI.
- Everything runs as systemd **user** services/timers. No cloud scheduling in v1.
- Voice hotkey capture via **raw evdev** (settled — compositor hotkey APIs don't
  expose keyup, breaking hold-to-talk). Text injection via ydotool — installed
  and user-verified working 2026-07-11 (if ydotoold won't start, see the
  /dev/uinput quirk under Known quirks).
- Claude Code with Fable 5 is both the builder and the agent runtime.

## Locked-in architecture decisions (do NOT re-evaluate)

1. **Single dispatch path** — the dispatcher is the ONLY component that invokes
   Claude. Voice, dashboard, timers, queue files are all clients.
2. **Quick vs agentic routing** — quick Q&A streams immediately; agentic →
   `claude -p` headless with per-task-type `--allowedTools`, `--max-turns`, spend
   cap. Prefer read-only scopes; never blanket permission-skipping unattended.
   (The quick path originally streamed via the Messages API; **superseded
   2026-07-27** — both paths now run `claude -p` on the subscription login. The
   routing distinction is unchanged; see "Operational notes (Claude auth)".)
3. **Refusal fallback** — headless `stop_reason: "refusal"` → auto-retry on
   configurable fallback model (default a current Opus/Sonnet), log both attempts.
4. **Memory** — markdown vault for context/notes/briefs (read natively from
   filesystem, no MCP for own files); SQLite for structured data. Nightly backup.
5. **Life areas as plugins** — one dir per area under `areas/` (SKILL.md, optional
   CLAUDE.md, agent prompts). Adding an area requires zero core changes.
6. **Voice plugin ABCs** — `WakeWordEngine`, `STTEngine`, `Brain`, `TTSEngine`;
   engines swappable via one config line; models load once, stay warm.
7. **Barge-in** — Silero VAD listens while Jarvis speaks; user speech cancels TTS
   AND aborts the in-flight Claude call, <200ms target. Plain VAD endpointing —
   no semantic turn detection.

## Component stack (settled)

FastAPI + SQLite (dispatcher) · React/Vite SPA over REST + SSE/WS (ui) ·
evdev F9 trigger · sounddevice 16kHz mono · faster-whisper `small.en` int8 ·
Silero VAD · openWakeWord · Piper TTS (Kokoro-82M drop-in later via ABC) ·
mpv + yt-dlp (later) · Gmail MCP read-only (Phase C) · systemd user timers.

## Repo layout

**`ui/dist/` is committed on purpose and must NOT be gitignored** (learned
2026-08-02). The dispatcher serves the built SPA straight out of it, so a fresh
clone needs it. It was ignored *and* tracked, which looks harmless because git
keeps tracking existing files — but Vite emits content-**hashed** asset names,
so a rebuilt bundle is a new path the rule hides: you would commit an
`index.html` pointing at a file that isn't in the repo. Rebuild with
`npm run build` in `ui/` and commit the whole directory.

```
dispatcher/   FastAPI service, task router, claude -p runner, refusal fallback
jarvis/       voice pipeline (plugins/ = ABCs, engines/ = implementations, main loop)
areas/tasks/  SKILL.md, CLAUDE.md, agents/ (daily-brief.md, weekly-review.md)
vault/        markdown memory: notes/, tasks/, briefs/
data/         SQLite db + backups/
queue/        drop-a-markdown-file task queue
ui/           React SPA
references/   cloned reference repos (gitignored)
systemd/      user unit + timer files, install script
context/      STATE.md + sessions/ — session continuity files (see protocol above)
config.yaml   single config: engines, models, budgets, fallback model, trigger key
```

## Reuse before reinventing

Clone reference repos into `references/`, read, and lift specific code/patterns
(license check + attribution comments). Never adopt as dependencies.

| Source | Lift |
|---|---|
| OpenClaw | streaming sentence-by-sentence TTS loop; markdown/URL sanitizer pre-TTS |
| OpenVoiceOS | plugin ABC discipline; warm long-lived model services |
| TaylorHuston/local-life-manager | life-area skill layout, vault structure |
| eleanorkonik build-a-dashboard gist | queue-dir orchestration; SQLite pragmas/backup habits |
| hoangsonww/Claude-Code-Agent-Monitor | ideas only: reading session JSONL for agent monitor |
| LiveKit Agents / Pipecat | barge-in pattern (reference only) |
| NousResearch/hermes-agent (MIT) | learning-loop review prompts, skill lifecycle/curator, memory guidance (Phase G) |
| open-jarvis/OpenJarvis (Apache-2.0) | observability: TTFT/ITL stats, run_steps timeline, X-ray footer (Phase H); **also** `agents/executor.py` (settle the run in a `finally`), `agents/errors.py` (classify_error with an explicit default), `agents/loop_guard.py`. **Not a voice reference** — it has no VAD, wake-word, mic or barge-in code at all (checked 2026-08-13) |
| mem0 / graphiti / letta (Apache-2.0) | extraction prompts, bi-temporal KG schema, core-block + sleep-time memory (Phases F/J) |
| khoj (**AGPL — patterns only, NEVER lift code**) | ingest chunking/hash-diff, NL automations, notify-or-not gate (Phases F/I) |

If a pattern isn't covered, search GitHub for prior art first and report findings
before writing from scratch.

## Working style

- Ask before adding any dependency not listed in the stack.
- Boring, readable code — one maintainer. Each component independently testable;
  smoke tests minimum.
- Deferred (do not build): other life areas, cloud Routines, multi-agent fan-out.

## Operational notes (learned Phase A)

- Run the dispatcher: `.venv/bin/python -m dispatcher.main` (port 8765).
  Tests: `.venv/bin/python -m unittest discover tests`. Acceptance:
  `scripts/smoke_phase_a.sh` (needs the server running).
- The `claude` command is a zsh alias; subprocesses must exec
  `/home/ayra/.local/bin/claude` with `CLAUDE_CONFIG_DIR=~/.claude-per`
  (set in config.yaml). This CLI (2.1.201) has **no `--max-turns`**; guardrails
  are `--allowedTools` + `--max-budget-usd` + wall-clock timeout.
- A bare `claude -p` run carries ~$0.15–0.25 notional cost (system-prompt
  overhead) — budget caps below that always trip `error_max_budget_usd`.
- **Subscription auth only** — no API-key path exists any more (removed
  2026-07-27). See "Operational notes (Claude auth)" below; a stray
  `ANTHROPIC_API_KEY` in the environment is actively harmful, and is stripped.

## Operational notes (learned Phase B)

- Run Jarvis: `.venv/bin/python -m jarvis.main --mode ptt|wake|both` (needs the
  dispatcher up). Dictation: `python -m jarvis.dictate`. Models via
  `scripts/setup_voice.sh`; headless acceptance: `scripts/smoke_phase_b.py`
  (TTS-generated speech drives wake/VAD/STT — no mic needed).
- **openwakeword pinned at 0.4.0** (0.5+ needs tflite-runtime; no Python 3.14
  wheels). 0.4.0 bundles hey_jarvis ONNX in-package. Its `reset()` doesn't
  clear the mel-spec buffer — engine flushes 2s of zeros instead (else stale
  audio re-fires the wake word).
- **Silero VAD v5 ONNX needs 64 context samples** prepended to each 512-sample
  frame (jarvis/vad.py handles it); bare 512 frames give near-zero probs.
- **Barge-in echo caveat**: without echo cancellation the mic hears Jarvis
  itself through speakers. Use headphones or PipeWire echo-cancel
  (`pactl load-module module-echo-cancel`) until tuned.
- Warm-load times (this CPU): all four models ~3s; STT of a short utterance
  ~1.1s; TTS starts instantly. Quick-path first delta ~3-5s — that is the
  floor on this path (the Claude Code system prompt is re-sent every cold run).
- **Trigger layout (updated 2026-07-22)**: `mission-jarvis` runs `--mode both`
  — "hey jarvis" (wake word) **and hold-`ptt_key`** (default `KEY_RIGHTCTRL`)
  both reach the assistant through the same handler; `mission-dictate` owns
  hold-`trigger_key` (F9, speech typed at cursor via ydotool). The two keys
  MUST differ — both services read the raw evdev stream, so a shared key fires
  dictation and the assistant at once (main.py logs a warning if they match).
  PTT needs no VAD: the key edge is the endpoint, and press/release each play a
  short beep (`ptt_beep_ms`, 0 disables). Pressing PTT while Jarvis is speaking
  is an explicit barge-in (stops playback + cancels the in-flight call).

## Operational notes (learned Phase F)

- Memory API on the dispatcher: `GET /memory/search?q=…` (FTS5 BM25 over the
  vault index + episode log; `file`/`after`/`before` filters),
  `POST /memory/reindex`, `GET /memory/blocks`, `POST /memory/consolidate`.
- Core blocks `vault/memory/{USER,MEMORY}.md` are injected into every quick
  and agentic prompt (char budgets in config.yaml, enforced at prompt build).
  Edit them by hand freely; the nightly agent and the memory area also edit
  them. Per-project notes go in `vault/memory/projects/` (searchable, not
  auto-injected yet).
- Every settled task (except cancelled, excluded sources, and the
  consolidator itself) appends a row to the SQLite `episodes` table — raw
  capture is LLM-free. `mission-memory-consolidate.timer` (02:30, before the
  backup) exports unconsolidated episodes to `data/consolidation/` (kept as
  audit trail, gitignored) and queues the agent from
  `areas/memory/agents/consolidate.md`. Episodes are marked at hand-off, so
  a failed run needs a manual replay against the export file.
- The vault FTS index re-syncs on dispatcher startup (`reindex_on_start`) —
  mtime prefilter + per-chunk hash diff keeps that cheap.
- Verifying memory recall: use a **fresh source name** — within
  `quick_session_idle_minutes` the same source resumes its CLI session, which
  can answer from conversation context and mask whether block injection works.

## Operational notes (learned Phase G)

- Learning loop: an agentic run finishing 'done' with `num_turns >=
  learning.reflection_min_turns` (12) auto-queues a **reflection** task that
  resumes the run's CLI session (`--resume`) with `Edit(areas/**)`-scoped
  tools and the Hermes-derived review prompt in `dispatcher/reflection.py`.
  "Nothing to save." is a normal outcome.
  Reflections never reflect, never enter episodes, and never match areas
  (`match_area=False` — the prompt's own text hits area triggers otherwise;
  learned live).
- Skill telemetry: `skill_usage` bumps on every dispatched task with an area
  and on mtime-detected area edits by reflection/learn runs. `GET /skills`
  lists areas + counters; `POST /skills/curate` runs the deterministic
  lifecycle (stale 30d → archived 90d later, move to `areas/.archive/`,
  never delete); monthly timer `mission-skill-curator` (1st, 04:00).
  Protected: pinned rows, `learning.curator.protected`, timer areas.
- `POST /learn {request, source}` authors a skill via the `learn` area; an
  empty request distills the source's recent quick conversation (session
  resume). Voice: "learn this as a skill".
- `claude -p --resume` works from the agentic runner too (same session store
  as the quick path). **It is not, however, "nearly free"** — the original
  $0.019 measurement never reproduced: two real reflections averaged **$0.108**
  (see the meta-task cost note under Phase I). Every cold `claude -p` re-sends
  the ~16-18k-token Claude Code system prompt whether or not it resumes, so the
  resume buys conversational context, not a cheap run. Reflection is
  deliberately left on `models.quick`, unlike the gate/parse tasks: it authors
  skills, so quality outranks the difference.

## Operational notes (learned Phase H)

- Agentic runs now use `--output-format stream-json --verbose`: every message
  becomes a `run_steps` row (init/text/tool_use/tool_result/result, clipped
  summaries, elapsed_ms) and a live SSE `step` event — the dashboard shows
  the current step under running rows and the full timeline on expand
  (`GET /task/{id}/steps`). The result object carries the same fields the
  old json format did.
- Quick runs record `ttft_ms` / `itl_p95_ms` / `tokens_per_s` (additive
  `runs` columns via the idempotent MIGRATIONS loop in db.py). First real
  number: TTFT ≈ 2.6s, and later measurements put it around 3-3.8s.
- `GET /stats?days=N`: tasks by status, per-source cost, success rate,
  quick-latency averages, recent reflection outcomes. Dashboard
  "Observability" widget renders tiles + a "Jarvis learned" strip
  (reflections that saved something).
- **RESOLVED 2026-07-19 (Layer-13 probe)**: the headless CLI auto-permits a
  **sandboxed read-only Bash** even when Bash is absent from
  `--allowedTools` — cwd-scoped (reads outside the repo blocked, e.g.
  /etc/passwd), writes blocked everywhere (in-repo and out). Since granted
  tools should mean exactly those, both runners now pass
  `--disallowedTools Bash` whenever Bash wasn't granted
  (runner.grants_bash); verified blocked through the live dispatcher.

## Operational notes (learned Phase I)

- **Ingest formats (updated after dep approval)**: `.pdf`/`.epub` extract
  text per page (pymupdf), textless pages OCR within a 10-page/file budget;
  images (`.png/.jpg/.jpeg/.webp/.tiff`) OCR via a lazy rapidocr singleton
  (~1.5s each — a big image dump slows one reindex). Only docx/doc/gif stay
  `dep_gated`. An uninstalled dep degrades back to counting, never breaks
  the walk.
- **Automations**: `POST /automations {request}` or any schedule-phrased text
  ("every morning, …") through `POST /task` mode=auto (nl_detect divert;
  questions never divert) → one LLM parse (task_type `automation-parse`,
  ~$0.06) → mechanical validation → `automations` row. An **in-dispatcher
  asyncio scheduler** (30s checks — deliberate deviation from the plan's
  systemd timer: the dispatcher must be up for tasks anyway, and this gets
  catch-up for free via past-due next_run_at) submits due rows as
  source=`automation` tasks. Schedule kinds: daily/weekly/interval/once;
  local-time fields, UTC next_run_at; 'once' rows spend themselves
  (next_run_at NULL). Re-enable via toggle recomputes next_run_at so a stale
  past-due row can't instant-fire — **false for `once`, the one kind where it
  matters** (found 2026-08-13, unfixed): `set_automation_enabled` writes
  next_run_at only when it is not None, and `next_run_iso` returns None for a
  lapsed `once`, so the stale timestamp survives and it fires within 30s. Empty/absent `automations:` config block
  disables the whole feature (that's what keeps unit tests LLM-free).
- **Notify gate**: 'done' automation/timer results run a `notify-gate` quick
  task (self-contained prompt: request + clipped result inlined)
  → `NOTIFY: <summary>` fires an SSE `notify` event
  (with `speech`) + notify-send; `SKIP:` is logged + `notify_skipped` event
  only. Fail-open: gate breakage notifies with a generic summary. done/failed
  events suppressed by policy carry `surface: false`; the jarvis brain
  honors both — **reflection/consolidation completions are no longer spoken**
  (before Phase I every agentic 'done' was announced when jarvis was up).
- **Meta-task cost (corrected 2026-07-26 — the old "warm resume is nearly
  free" claim was wrong).** The one-off $0.009 gate measurement on 2026-07-18
  never reproduced: the next 11 runs averaged **$0.141**. Two causes, both
  measured, both fixed: (1) resuming the settled run replays a long agentic
  transcript as input for a one-line verdict — $0.141 resumed vs $0.103 fresh,
  so `_notify_gate` no longer passes `resume_session_id`; (2) the real floor is
  the Claude Code system prompt itself (~16k cache-creation tokens on **every**
  cold `claude -p`), so the model rate dominates — `models.meta`
  (haiku) + `models.meta_task_types` now route the pure-classification tasks
  (notify-gate, automation-parse, media-parse), each of which has mechanical
  validation downstream. Live after the change: $0.0405 vs $0.189 for the
  preceding real gate run. `graph-extract` is deliberately excluded — its
  output is fact text that lands in the knowledge graph. Clearing `models.meta`
  restores the old routing. The floor itself is **permanent** on this path —
  the Messages API would have avoided it (measured $0.00051 vs $0.14052 a
  question) but was removed 2026-07-27; `models.meta` is the lever that remains.
- Internal quick sources (`automation`, `automation-parse`, `notify-gate`)
  are excluded from quick-session continuity — unrelated machine tasks must
  not chain each other's CLI sessions. The gate resumes via metadata
  `resume_session_id`, which stream_quick now honors ahead of per-source
  continuity.
- **Ingest formats**: `.txt/.text` (paragraph chunks) and `.html/.htm`
  (stdlib-parser → markdown-ish → heading chunker; bookmark links keep URLs)
  now index alongside `.md`; drop exports in `vault/inbox/` (see its README).
  PDF/images are counted as `dep_gated` in reindex stats, not indexed.
- First scheduled 02:30 memory consolidation ran clean on 2026-07-19 (the
  Phase F timer's first unattended fire).

## Operational notes (learned Phase J)

- **Hybrid search**: `/memory/search` fuses FTS5 BM25 + sqlite-vec KNN with
  RRF (k=60) for entries AND episodes; deterministic filters (`file`,
  `after`, `before`) force the FTS-only path (khoj filters-before-vector).
  Missing sqlite-vec or `embeddings.enabled: false` degrades everything to
  FTS-only silently. Verified live: "preferred coding tool" (zero FTS hits)
  surfaced the USER.md Neovim block via vectors alone.
- **Embeddings**: fastembed bge-small-en-v1.5 int8 (384-dim; ~20s first
  model load incl. download into `data/models/fastembed/`, warm after).
  vec0 tables key on rowid == entry/episode id (deletes stay one statement).
  Backfill runs at startup (after reindex), after `POST /memory/reindex`,
  and every `refresh_minutes` (15) — first live pass embedded 49 entries +
  10 episodes in ~0.4s once warm.
- **Knowledge graph**: nightly fact extraction (`graph-extract` quick task,
  mem0/graphiti-derived JSON prompt) rides the same episode export as the
  consolidation agent — spawned by `/memory/consolidate`, deterministic
  apply in `dispatcher/graph.py`. Facts are sentences linked n-ary to
  entities; invalidation sets `invalid_at`/`expired_at`, never deletes.
  `scope=graph` in `/memory/search` returns matching facts + 1-hop
  neighbors. Weekly `mission-memory-reconcile` timer (Sun 04:30) invalidates
  duplicates/contradictions; "nothing_to_reconcile" while < 2 active facts.
- Meta task types `graph-extract`/`graph-reconcile` are excluded from
  episode capture, notice surfacing, and quick-session continuity — same
  hygiene as the Phase I gate/parse tasks.

## Operational notes (ask-about-my-screen)

- Flow: GNOME shortcut `<Super><Alt>a` → `python -m jarvis.ask_screen` →
  xdg-portal **area-select** screenshot (jeepney D-Bus; grim doesn't work on
  this GNOME Wayland) → PNG moved to `data/screenshots/` (keep_last=20) →
  chromium `--app` popup at `/ask?shot=<id>` — typed or mic question,
  streamed answer, follow-ups resume the CLI session (source
  `screen:<shot_id>`, image stays in context, resumed turns ~6× cheaper).
- Server seams: `POST /stt` (raw-body webm/wav → lazy server-side Whisper,
  ~200-300MB, first call ~3s), `GET /screenshots/{name}`
  (traversal-guarded), `GET /ask`. The quick path wraps the prompt with a
  Read-the-image instruction **only on fresh sessions** (first turn or
  vanished-session retry); DB `text` keeps the raw question.
- Setup: `scripts/setup_ask_screen.sh` (installs jeepney — approved dep —
  and the gsettings binding; idempotent). Acceptance:
  `scripts/smoke_ask_screen.sh` (incl. Piper-synthesized real-audio /stt
  check and one real image-reading quick task).
- Hold-F9 dictation types into the popup textarea for free (it's a focused
  text field); the mic button is the built-in alternative.

## Operational notes (media control / Spotify)

- **Deterministic by default**: `dispatcher/spotify.py` `detect()` is a pure
  parser (sibling of `automations.detect`) — "play X", "pause", "skip",
  "what's playing", "volume 40" never reach Claude, so they cost nothing and
  land in well under a second. The divert chain (automation → media → desktop)
  lives on **`Service.try_divert()`**, not in the HTTP handler — moved there
  2026-08-02 because `automations.fire` and the queue watcher call
  `svc.submit()` directly, so a *fired* "play jazz" automation had been going to
  Claude as an agentic task every morning instead of to Spotify. `mode=auto`
  only, media after automation so "every morning play jazz" still becomes a
  standing automation. Over HTTP a divert streams and records nothing (as
  before); through `submit()` it becomes a task **born settled** (`task_type:
  divert`, no run row) because those callers need a task dict and an audit
  trail. Diverts are **never gated** (`notify.NEVER_GATE_TASK_TYPES` — the
  executor already wrote the one-line summary) and never captured as episodes
  (else the graph eats an identical "play jazz" nightly). An automation can
  never create an automation.
  **"Announced" was aspirational until 2026-08-08**: `_settle_divert` fires the
  plain `done` event, but that carries `kind="quick"` and the voice client only
  speaks `kind == "agentic"` or a `notify` event — and nothing on the path
  called `send_desktop`, so a fired "play jazz" ran in silence and
  `NEVER_GATE_TASK_TYPES` was unreachable dead code (with a passing test over
  it, because the test called `surfacing()` directly). Delivery now goes through
  `_deliver_notice`, the same `notify` event the inbox watcher uses.
  **A divert is also no longer automatically `done`** — **but only for the parked
  and denied cases** (corrected 2026-08-13): a missing app, a refused URL scheme
  and an unknown verb still report `done`, because `run_desktop_intent` and
  `media_command` hardcode it while `run_intent` returns failures as sentences.
  That is what lets the parser's over-claiming swallow a request silently.
  `try_divert` returns `ok`
  from the executor, so a parked confirmation (nobody watches the `queue` /
  `automation` sources — it just expires), a denied verb, or an unresolvable
  play settles `failed`. A *failed automation parse* now falls through to normal
  routing rather than consuming the request: the parse is itself a quick task,
  so failing is the normal outcome during a plan-cap window, and the request was
  being left neither scheduled nor run. The whole chain is wrapped — a divert is
  an optimization, and raising there 500s `POST /task` and files a queue file
  under `.failed` as terminal. Non-music text returns None and routes as before;
  a trailing "?" vetoes the divert (except "what's playing?"), and a VETO
  list guards idioms ("play devil's advocate", "play it safe").
- **LLM fallback**: music-shaped text the parser can't resolve ("put on
  something chill") returns the `MAYBE` sentinel → one `media-parse` quick
  task that chooses **search words only**; the action is always executed by
  the deterministic path. Deliberately not an area — an area would need Bash
  to reach D-Bus, which `security.privileged_areas` exists to prevent.
  `media-parse` is excluded from quick-session continuity and never surfaced,
  same hygiene as `automation-parse`/`notify-gate`.
- **Playback is local**: MPRIS over jeepney (`org.mpris.MediaPlayer2.spotify`,
  `OpenUri`/`Play`/`Pause`/`Next`/`Previous`, writable `Volume` property).
  A play command launches the flatpak client if its bus name is absent
  (`media.launch_cmd`, ~20s wait); transport commands against a dead player
  just say "Spotify isn't running". All blocking calls run in
  `asyncio.to_thread`. jeepney returns D-Bus errors as *replies*, not
  exceptions — `_check()` handles that (same trap as `jarvis/ask_screen.py`).
- **Search needs credentials, not Premium**: `GET /v1/search` under the
  **client-credentials** flow (app-only; no user OAuth, no redirect dance).
  `scripts/setup_spotify.sh` writes `data/spotify.json` (gitignored, mode
  600); `SPOTIFY_CLIENT_ID`/`_SECRET` env override it. Missing credentials
  degrade gracefully — transport still works, `play X` says to run the setup
  script. Premium (including **Premium Student**, which is a full Premium
  tier) is only needed for the user-OAuth Web API playback endpoints, which
  this design deliberately avoids; the trade is no access to the user's own
  playlists/liked songs.
- Seams: `POST /media {command}`, `GET /media/state`. `run_intent` never
  raises — every failure becomes a speakable sentence. Nothing in `jarvis/`
  changed: the divert streams a `delta`, which the voice brain already speaks.

## Operational notes (learned in the codebase-review remediation)

- **`simulate_refusal` was outside the boundary until 2026-08-08.** It forces
  the refusal fallback — a **second** run of the same task on `models.fallback`
  (an Opus) — and `sanitize_untrusted_metadata` passed it straight through while
  correctly stripping `allowed_tools` and clamping the budget. Anything that
  could reach `POST /task` (dashboard, phone over Tailscale Serve, a queue file)
  could therefore double the cost of every agentic run. Now stripped like the
  rest, with one opt-in: `security.allow_simulate_refusal` (default false),
  which `scripts/smoke_phase_a.sh` documents turning on and back off — that
  script is the only legitimate caller and it drives the check over HTTP.
- **The quick path silently ignored `metadata["allowed_tools"]`** (fixed
  2026-08-08): tools came from the matched area only, so a trusted internal
  spawn that asked for tools got **none**, plus `--disallowedTools Bash`.
  `inbox.summarize` ships `["Read","Glob","Grep"]` with a prompt saying "Read
  it", so it was spending a real call per file on an answer the model could not
  ground. Honoring the key here is safe for the same reason the agentic resolver
  already does: H1 strips it *before* the row is written, so anything still on a
  row came from a server-internal spawn.
- **The queue watcher claims a file before submitting** (2026-08-10). It used to
  submit and *then* rename into `.processed`, with the rename inside the same
  `try` and `OSError` on the transient-retry whitelist — so a rename failure
  (read-only mount, ENOSPC, permissions, a synced/FUSE queue dir) left the file
  in place and the next poll ran the whole task **again**, up to
  `queue_max_retries` times, with real side effects and no log line saying a
  task had already gone out. Order is now claim (`*.claimed`) → submit → file.
  Past the submit, nothing may return the file to the queue: a failed *filing*
  logs loudly and leaves it claimed, because "the task ran but wasn't recorded"
  is recoverable and "the task ran four times" is not.
- **A wall-clock timeout is NOT retried** (2026-08-10). `runner.run_once` used to
  retry it like a spawn error. A run that hits `budgets.timeout_s` was usually
  working, so the retry re-ran the same prompt with the same write grants over
  files the first attempt had already edited — and `_attempt` raises before any
  `result` event, so the timed-out attempt reports no cost/session/steps while
  sharing the single runs row. Spawn failures stay transient.
- **A cancel that already landed wins over the subprocess kill's error**
  (2026-08-10). An HTTP quick task is never in `svc.bg`, so `cancel()` only kills
  the process — after writing 'cancelled' — and the kill surfaces as `failed` +
  "claude exited -9", which then overwrote the run row on **every voice
  barge-in**. The task row was always correct; the run row was not.
- **Latency columns are only written for a real stream** (2026-08-10). When the
  CLI emits no incremental text, `quick.py` synthesizes one delta from the final
  result, so "TTFT" was the whole run duration — sitting in `/stats`
  quick-latency averages looking like a genuine measurement. `meta["streamed"]`
  now gates `telemetry.stream_stats`.
- **Consolidation episodes roll back on every non-`done` path** (2026-08-10):
  `Service._rollback_episodes` is called from the failed settle, the cancel
  branch, the outer crash handler, and a startup sweep
  (`db.orphaned_consolidation_episode_ids`) for a restart mid-run — episodes are
  marked at hand-off, so any uncovered path stranded that batch forever.
  `/memory/consolidate` is also serialized now (it was a read-then-mark race:
  two callers could both claim the same set and both submit an agent run).
- **The two divert renderers deliberately disagree on one point** (2026-08-10).
  Both derive status from the executor rather than hardcoding 'done' (the
  `main.py` renderer didn't until this date, so an unresolved play was reported
  to the dashboard as a success). But a **parked confirmation** is 'done' over
  HTTP and 'failed' through `submit()`: over HTTP the caller is present, gets
  `confirm_id` and answers via the ConfirmBar or by voice, so it is a live
  interaction; on the queue/automation path nobody is watching that source and
  the intent simply expires. Tests pin both sides.
- **`Restart=on-failure` needs a widened StartLimit to mean anything**
  (dispatcher 2026-08-10, both voice units 2026-08-11). systemd's default is 5
  starts per **10s**; at `RestartSec=5` only ~2 land in that window, so the
  limit never trips and a crash-on-startup restarts forever reporting
  "activating", never `failed`. On `mission-jarvis`/`mission-dictate` that is
  worse than on the dispatcher: nothing else tells you the assistant has gone
  deaf. All three now run `StartLimitIntervalSec=300` / `StartLimitBurst=5` /
  `RestartSec=10`, and `tests/test_dispatcher.py` pins the arithmetic
  (`burst * RestartSec < interval`) for every restarting unit, since the
  mistake has now been made twice.
- **The graph tracks its own hand-off** (`episodes.graph_extracted_at`,
  2026-08-11). `/memory/consolidate` marks a batch consolidated and spawns
  *both* the consolidation agent and graph extraction over it — and a failed
  consolidation rolls `consolidated_at` back, handing the extractor episodes it
  had already digested. The graph is now marked separately and **only on
  success**, `episodes_needing_graph()` filters the batch, and the extractor is
  rendered its own subset via `memory.render_episodes()` rather than reusing
  the consolidation export. `add_fact`'s duplicate-rejection stays as the
  backstop it was always meant to be, not the mechanism.
- **A parked confirmation now expires in the UI too.** The SSE `confirm` event
  carries `timeout_s`; `ConfirmBar` counts it down and removes the row. Before
  that the banner kept offering **Yes** on an id the dispatcher had already
  dropped (and survived a dispatcher restart, which drops every pending
  intent) — clicking it reported "expired" with no prior warning, on the two
  verbs the confirm plane exists to guard. Verified in a browser against an
  isolated dispatcher with `confirm_timeout_s: 8`: 7→1 then gone.
- **Trust boundary (H1/H2)**: metadata from an external source
  (api/queue/voice/ui/screen) can NARROW tools/budget but never WIDEN them —
  `sanitize_untrusted_metadata` drops `allowed_tools`/`resume_session_id` and
  clamps `max_cost_usd` to the cap; only server-internal spawns pass
  `trusted=True`. Areas strip Bash/unscoped Edit-Write from SKILL.md
  frontmatter at load unless listed in `security.privileged_areas` (empty by
  default) — a reflection/learn run editing `areas/**` can't grant itself Bash.
  Read that invariant precisely (2026-08-02): it bars **caller-supplied**
  grants. A caller may still *select among* operator-authored on-disk manifests
  via `metadata.task_type` (→ `config.yaml` `task_types`) or `metadata.agent`
  (→ `areas/<area>/agents/<agent>.md`), and those can be broader than the
  read-only default. That is deliberate — it is how the timer agents get their
  grants at all — and safe because the manifests are repo content at the same
  trust level as SKILL.md, with the agent path sanitized at load.
- **…but every path to those grants must go through the sanitizer, and one
  didn't** (fixed 2026-08-02). `memory.build_consolidation` re-parsed
  `areas/memory/agents/consolidate.md` itself instead of calling
  `AreaRegistry.agent_tools`, and its metadata is submitted `trusted=True` — so
  a reflection run (which holds `Edit(areas/**)`) writing `- Bash` into that
  file handed the nightly consolidation a shell, exactly the escalation H2
  exists to stop. Grants now come from `AreaRegistry` on both paths; the local
  frontmatter parse is only for the prompt body and `task_type`.
- **The memory system was feeding on its own housekeeping** (found + fixed
  2026-08-02). `should_capture` excluded the consolidator, reflections and the
  gate/parse tasks — but **not `daily-brief`/`weekly-review`**, whose episode is
  "I wrote a file". So the nightly `graph-extract` kept reading those back as
  knowledge: **every fact in the knowledge graph** was a self-observation
  ("the daily-brief automation continued writing successfully through 07-31"),
  each night invalidating the previous night's copy of itself — 6 facts, 4 of
  them already superseded, zero about the user. Both task types are now
  excluded. Nothing is lost: a brief's *content* reaches memory through the
  vault index, which ingests `vault/briefs/` already. The 6 facts were purged
  and the pre-fix episodes marked consolidated (DB backed up first, every row
  asserted to match a self-observation/test-artifact guard).
- **The daily brief skips the model on a quiet day** (2026-08-02).
  `dispatcher/brief.py` is a deterministic pre-check in the same family as
  `spotify.detect`/`desktop.detect`: no open tasks in `vault/tasks/` and no
  note touched in `brief.quiet_notes_days` (3) → `scripts/run_agent.py` writes
  the brief itself and never submits a task. It had been spending ~$0.40 and
  8-11 turns every morning to write "clean slate", every day from 07-24 to
  08-01, because the vault holds one *done* task from 07-06 and two notes from
  07-05. Costs are notional on the subscription, but the **quota** is real and
  the plan cap has killed the assistant twice. Any material at all falls
  through to the real agent; an unreadable task file also falls through (fail
  toward doing the work). `brief.skip_model_when_quiet: false` restores the old
  behavior.
- **The daily brief is email-free on purpose.** A Gmail MCP server *is*
  connected in `~/.claude-per` (so `mcp__claude_ai_Gmail__*` shows up in every
  agent's tool list), but the user declined to feed mail into the brief
  (2026-08-01). The agent's old `mcp__gmail` grant matched nothing, so it
  requested Gmail, got denied, and burned a turn on **every single run** since
  Phase C. Both the dead grant and the prompt's Email section are gone, and the
  prompt now says explicitly not to call the tools it can see. To re-enable:
  grant `mcp__claude_ai_Gmail__search_threads` + `…__get_thread` and restore the
  section — no code change needed.
- **A `Persistent=true` catch-up can write tomorrow's brief.** The box was off
  at 07:30 on 07-29; the timer caught up at 00:42 *on the 30th*, so the agent's
  `{{DATE}}` was already 07-30 — there is no 07-29 brief, and the 30th's real
  run then found its file already written. Harmless once the denial fix above
  is in, but that's why brief dates can skip a day.
- **Timer agents get their tools from disk, not the wire** (learned the hard
  way 2026-07-24): `scripts/run_agent.py` sends `metadata.agent = <name>`, and
  the dispatcher resolves `areas/<area>/agents/<name>.md`'s `allowed_tools`
  itself (`AreaRegistry.agent_tools`, slug-guarded, sanitized like SKILL.md).
  Sending the grant in metadata instead (the old way) had it stripped by the
  trust boundary — the daily brief silently wrote nothing for three days
  (2026-07-21..23), all three runs falsely `done`.
- **Denied tools no longer masquerade as success**: the headless CLI reports a
  missing `--allowedTools` grant as an ordinary `is_error` tool_result and lets
  the model continue, so a run that was denied every write still ended
  subtype=success. `runner.py` now flags denials; a denial that stopped the run
  producing the output file its prompt names (mtime vs a pre-spawn timestamp) →
  `failed`; a tolerable denial → `done` with the denial recorded in
  `denied_tools` and logged as a warning.
- **…but only a denial of a tool that could have written the file counts**
  (fixed 2026-08-01). The mtime test can't tell "a denial blocked the write"
  from "the agent correctly decided no edit was needed", and the daily brief hit
  the second case twice (07-23, 07-30 — a post-midnight catch-up run had already
  written the file, so the real run made no edit and a harmless Gmail denial
  promoted it to `failed`, $0.51 wasted and no notification). `runner.
  denial_could_block_output` parses the tool name out of the denial and only
  treats `Write/Edit/MultiEdit/NotebookEdit/Bash` as capable of explaining a
  missing file; an **unparseable** denial stays fatal, because a silent false
  success is the failure mode session 17 exists to prevent.
- Other review fixes now live: `/stt` body-size cap + origin guard, real
  quick-path cancel (proc registration + cancelled-guard), SQLite
  `busy_timeout=15s`, memory block-file write allowlist + scoped summarize
  Write, **failed consolidation surfaced + episodes rolled back** to the pool,
  orphan-vector sweep, DST-correct `zoneinfo` for automations + a
  memory-statement divert veto, Piper both-file atomic guard.

## Operational notes (phone access — Tailscale + PWA)

- **Chosen option** (research in `future/phone-access-and-feature-gaps.md`):
  Tailscale, because it needs **no token-auth middleware and opens no public
  port** — the dispatcher stays bound to `127.0.0.1` and **Tailscale Serve**
  proxies it as tailnet HTTPS. Only your own devices reach it. ngrok was
  rejected (its free tier withholds auth, forcing bearer-token middleware).
- **The one code change**: the origin guard (`dispatcher/main.py`
  `_origin_is_local` / `guard_origin`) is a **CSRF check, not auth** — it 403s a
  state-changing request carrying a *non-local* `Origin`. The dashboard loaded
  over `https://<box>.<tailnet>.ts.net` POSTs with exactly that Origin, so the
  hostname must be trusted: `security.public_hosts` (config.yaml, a string or
  list) feeds `cfg.public_hosts` into the guard alongside loopback + `cfg.host`.
  It is **only** the origin allowlist and grants no authentication — Tailscale is
  the access control. Verified: an allowlisted host passes, a different
  cross-origin site still 403s (tests/test_ask_screen.py).
- **Serve unit**: `systemd/mission-tailscale-serve.service` (oneshot,
  RemainAfterExit) runs `tailscale serve --bg --https=443 http://127.0.0.1:8765`.
  Prereqs (operator perms so `serve` needs no sudo, HTTPS/MagicDNS enabled) are
  set by `scripts/setup_tailscale.sh`, which is idempotent and **prints the exact
  `public_hosts` line** for the machine's MagicDNS name.
- **User-side steps** (nothing here can do them): `sudo pacman -S tailscale` →
  `scripts/setup_tailscale.sh` → paste the printed `public_hosts` line into
  config.yaml → `systemctl --user restart mission-dispatcher` → install the
  Tailscale app on the phone, join the same tailnet, open the URL.
- **PWA**: `ui/public/` holds `manifest.webmanifest`, `sw.js`, and PIL-generated
  icons (192/512/maskable/apple-touch, dark "MC" tile matching the dashboard
  theme); `ui/index.html` links them, `ui/src/main.jsx` registers the worker.
  The service worker is **deliberately minimal** — a live SSE dashboard, so it
  only makes the app installable + caches the shell/hashed assets and **never**
  touches `/events` or any POST/API GET. StaticFiles serves everything in
  `ui/public/` from `/` after `npm run build`; a defensive
  `mimetypes.add_type(".webmanifest")` in main.py keeps the content-type right on
  pre-3.14 interpreters. No new dispatcher routes were needed.

## Operational notes (inbox watcher — event-driven proactivity)

- Everything proactive before this was **clock**-driven (timers, the
  automations scheduler). `dispatcher/inbox.py` is the first thing that reacts
  to the world changing: drop a file in `vault/inbox/` and it's indexed and
  acknowledged without anyone asking. Config block `inbox:` (enabled, dir,
  check_interval_s 60, notify, summarize).
- **Polling, not inotify** — the dispatcher already polls for two other things,
  a minute of latency is irrelevant for "I saved a file", and inotify would
  mean a dependency plus watch-descriptor edge cases on a synced directory.
- **Two-phase settle**: a file fires only when its (mtime, size) is unchanged
  across two consecutive polls, so a large PDF still being written isn't
  indexed at half length. The first pass **seeds without firing**, so a restart
  doesn't re-announce the whole directory.
- `step()` returns **(arrived, removed)**. Removals trigger a reindex but no
  notification — without that a deleted file stays searchable until the next
  restart; arrivals get both. The notify branch must read **`arrived`**, not
  just sit under the `if arrived or removed:` that guards the reindex: until
  2026-08-02 it didn't, and deleting a file announced "Indexed **0 files** from
  your inbox — searchable now."
- **Deterministic by default**: arrival → reindex + embed + "Indexed X —
  searchable now" (desktop + SSE `notify`), which costs nothing.
  `inbox.summarize: true` additionally spends one read-only quick call per file
  to say what it is — opt-in, because cheap surprises are still surprises.
- Live-verified: file dropped → picked up on the next poll → reindexed
  (`added: 1`) → immediately searchable via `/memory/search` with no manual
  reindex → deletion swept its rows.

## Operational notes (degraded mode when the plan cap hits)

- The plan-wide session cap took the assistant out twice (sessions 7 and 10):
  the quick path failed, spoke the reset time, and stayed dead until the
  window rolled over. `dispatcher/offline.py` now answers from the local
  Phase F/J index instead — the same hybrid BM25+vector search `/memory/search`
  runs, **extractive only**: verbatim quotes with the source file named, no
  generation, because the one thing worse than "I'm rate-limited" is a
  confident sentence nobody wrote. Config: `memory.offline_fallback` (default
  true). Internal plumbing sources are excluded — a machine task wants a real
  failure, not a consolation paragraph it might act on.
- **The relevance anchor is the load-bearing part.** KNN always returns
  *something*: asked about "the airspeed velocity of a laden swallow", the
  index cheerfully handed back its three least-unrelated chunks, which then
  got quoted as "here's what I already have on it". So a degraded answer is
  only offered when **BM25 matched a real term** somewhere first; ranking
  still uses the hybrid path, so vectors keep floating the right chunk up —
  they just can't conjure a topic from nothing.
- **…and that sentence was false until 2026-08-08 — degraded mode was dead.**
  The anchor passed the *whole question* to `db.search_entries`, and
  `db.fts_query` joins terms with a space, which is FTS5 implicit **AND**. So it
  demanded every token, stopwords included, in one chunk: `'what is my preferred
  coding tool'` → 0 hits, `'preferred coding tool'` → 0 hits, `'Neovim'` → 1.
  Every natural-language question got "I checked my memory and didn't find
  anything relevant" with the answer sitting in `USER.md`. (The "preferred
  coding tool" success recorded under Phase J was measured through
  `/memory/search`, which never applies this anchor — that claim is still
  correct, it just never covered this path.) It now probes **one content word at
  a time** (`ANCHOR_TERMS` = 8, `any()` short-circuits); the swallow still gets
  nothing, because none of its content words are in the index. `about`/`did`
  joined STOPWORDS at the same time — per-word anchoring means anything left in
  that list can anchor alone, and both match this index by themselves.
  `tests/test_offline.py`'s fake DB ignored the query string entirely, so it
  could never have caught this; it now models AND semantics, and **9 of its 17
  tests fail against the pre-fix module**. `<!-- -->` comments are
  stripped from snippets (USER.md/MEMORY.md open with an editor instruction
  that otherwise eats the whole quote).
- **…and it is STILL bypassable — by contractions** (found 2026-08-13, unfixed).
  `_content_words` keeps `'` in the word charset, so `what's` survives as a
  content word while `what` is a stopword — and FTS5's unicode61 tokenizer
  *splits* on the apostrophe, so the probe matches any chunk containing "what's".
  Briefs and episodes are Claude prose full of contractions, so **"what's the
  airspeed velocity of a laden swallow?"** — the very example above, asked the
  way a person actually asks it — anchors and returns three unrelated briefs.
  `test_offline.py`'s `_tokens` models a tokenizer that *keeps* the apostrophe,
  the opposite of FTS5, so it still cannot catch this. Fix: `[a-z0-9]{2,}` plus
  the negative-contraction stems in STOPWORDS. Two measured traps for whoever
  does it: a "snippet must share a content word" filter kills 11 of 18 good
  snippets including the USER.md/Neovim hit, and a distance threshold is useless
  (gibberish scores L2 0.9076, *better* than the good query's best hit at 0.9181).
- **The BM25 leg of `/memory/search` itself has the same implicit-AND defect and
  is still unfixed** — `db.fts_query` (db.py:1070) returns 0 rows for every
  natural-language question, so "hybrid" search is pure KNN today.
- The run still settles **failed** (the model call really did fail, and the
  cost/latency stats stay clean); the `done` SSE payload carries
  `degraded: true` so a client can label where the text came from.
- This is the seam a **local model** would plug into later — that needs a
  dependency and a user decision; this needed neither.

## Operational notes (desktop control — computer-use T1)

- **Spike first, and it narrowed the tier** (2026-07-26, GNOME/Wayland here).
  `future/computer-use.md` assumed window list/focus/close via GNOME Shell
  D-Bus; it isn't reachable: `Shell.Eval` returns `(false, '')` (locked since
  GNOME 41) and `Shell.Introspect.GetWindows`/`.GetRunningApplications` return
  **AccessDenied**. Screen brightness is out too — this gsd exposes only
  `.Power.Keyboard`, and `/sys/class/backlight/*/brightness` is root-owned.
  Both need a dependency (a shell extension / `brightnessctl` + udev), so both
  are **deferred, not built**. Everything else in T1 shipped.
- **`dispatcher/desktop.py`** is the sibling of `spotify.py`: pure `detect()`
  parser → never-raises `run_intent()` → service divert, so `lock the screen`,
  `open firefox`, `system volume 40`, `what's on my clipboard` reach **no
  model**, cost nothing, and answer in well under a second. Verbs: status,
  volume, mute, lock, launch, open, clipboard_get, clipboard_set — all over
  binaries that were already installed (wpctl / loginctl / gtk-launch /
  xdg-open / wl-copy / wl-paste). **Zero new dependencies.** Deliberately not
  an area, for the same reason media control isn't: an area would need Bash to
  reach the session bus, which `security.privileged_areas` exists to prevent.
- **Volume phrasing is qualified on purpose.** `spotify.detect` owns bare
  "volume 40" (player volume) and the media divert runs first, so every desktop
  volume pattern demands system/master/computer — otherwise the two parsers
  fight over one sentence.
- **…and the qualified phrasings were thin until 2026-08-10.** Only four shapes
  parsed ("system volume 40", "set the system volume to N", "system volume
  up/down", "turn the system volume up"); everything else a person actually says
  — "turn **up** the system volume", "raise/lower/increase/boost the system
  volume", "make the computer louder", "volume up on my computer", "mute my
  computer" — fell through to a real `claude -p` call, i.e. ~$0.10-0.14 and ~3s
  to do what wpctl does for free. The widening keeps the qualifier rule intact:
  relative phrasings are five shapes (`_VOL_REL_RES`, split by sentence shape
  because a leading direction word is a verb and a trailing one is an adverb),
  absolutes gained named levels (`NAMED_LEVELS`: max/full/half/quarter/zero,
  "all the way up"), and mute gained "silence …" + "turn the sound off/on".
  Unqualified **"max volume" / "half volume" / "make it louder" are still left
  alone on purpose** — claiming them while bare "volume 40" goes to Spotify
  would split one sentence shape across two subsystems. `>100` still routes
  normally instead of clamping (likelier a misheard sentence than a request).
- **`machine`/`pc`/`speaker` qualify a noun but never stand alone** (review,
  2026-08-11). Adding them to `_SYS` made `_STATUS_RE`'s bare
  "<qualifier> status|state" shape match **"machine state"**, "pc status" and
  "what is the machine state" — an ordinary question about a state machine or a
  VM, answered with the volume report, filed done, never seen by a model. Same
  silent-swallow family as "open the door". The bare shape now uses `_SYS_CORE`
  (the words that were always in it); the wider `_SYS` only ever appears beside
  an audio noun or the lock.
- **`_DOWN_WORDS` is a denylist, so an unclassified direction word turns the
  volume UP** — on a verb whose policy is `allow`, i.e. no confirmation and
  nothing to notice. `_UP_WORDS` exists solely so a test can pin the partition
  of `_ADV`/`_VERB`; keep both in step when adding a direction word.
- **A negative test only pins what its inputs can actually exercise.** The
  first cut of `test_ordinary_english_is_not_a_volume_command` ("raise an
  exception", "lower my expectations") was written to pin the qualifier rule
  and pinned nothing: none of its inputs carries an audio noun, so they fail on
  the noun, not the qualifier — mutation-tested, the whole suite still passed
  with the qualifier made optional. `test_the_system_qualifier_is_what_keeps_
  them_apart` ("raise the volume", "lower the sound") is the one that kills
  that mutant.
- **Matching folds case; arguments must not** (fixed 2026-08-02). `_normalize`
  takes `fold=`, and the four arg-bearing patterns run `re.I` against the
  case-preserving string. Pulling the arg out of the folded one stored
  `copy Hello World to my clipboard` as `hello world`, and turned
  `open youtube.com/watch?v=dQw4w9WgXcQ` into a link to a **different video** —
  an argument is payload, not syntax.
- **Safety plane** (`computer.policy` in config.yaml): allow / confirm / deny
  per verb, failing **closed** — an unknown verb or an unrecognised policy
  value denies, and an absent/disabled `computer:` block denies everything
  (which is also what keeps the unit tests side-effect-free). Defaults are
  *not* "every write verb confirms": these verbs are reversible and low-stakes,
  and confirming "system volume 40" by voice every time would be worse than no
  feature. The two that do confirm are `clipboard_get` (the clipboard routinely
  holds passwords, and it's the one verb that reads user data into a model's
  context) and `open` (arbitrary URL = exfiltration channel + the obvious
  prompt-injection payload).
- **`open` hard-restricts schemes at the executor** (http/https/file), so it
  holds even if an operator sets `open: allow`. `desktop.scheme_of` is
  hand-rolled because urlparse is actively wrong here: the dangerous schemes
  are the *opaque* ones with no "//", so normalizing a bare host by prepending
  "//" makes urlparse read `javascript:alert(1)` as a host with an empty
  scheme — i.e. it silently passes the exact input the guard exists to stop.
  A unit test pins that regression.
- **Confirmations**: a `confirm` verdict parks the intent (in-memory,
  `confirm_timeout_s`, 120s) and fires an SSE `confirm` event instead of
  acting. Answer it two ways — the `ConfirmBar` banner above the dashboard
  grid, or **a bare "yeah"/"nope" by voice**, which only applies when that
  same source has one pending (a stray "yes" in conversation routes normally).
  Ids are single-use; a replay reports expired. A dispatcher restart drops
  pending confirmations, which is the safe direction to fail.
- Seams: `POST /desktop {command}`, `POST /desktop/confirm {confirm_id,
  approve}`, `GET /desktop/verbs` (what the tier can do and under what policy).
- **A systemd *user* service is not in a login session.** It lives under
  `user@1000.service`, so `loginctl show-session self` answers "Caller does not
  belong to any known session" from the dispatcher — the original `_status`
  swallowed that as a DesktopError and therefore *never once* reported lock
  state. Lock state now comes from the session bus
  (`org.gnome.ScreenSaver.GetActive`), which does work there; `screen_locked()`
  returns None for "couldn't tell" so it never reads as "definitely unlocked".
  `loginctl lock-session` (no ID) **does** work from that context — verified,
  accidentally, by locking the screen for real 2026-07-26.
- **Launching an app needed three fixes, not one** (2026-07-27 — `open firefox`
  had never once worked, while answering "Opening firefox." every time):
  1. **No display.** A user service only carries `DISPLAY`/`WAYLAND_DISPLAY` if
     it started *after* GNOME ran `systemctl --user import-environment`; at boot
     it doesn't, so the dispatcher's environment had neither. firefox printed
     "no DISPLAY environment variable specified" into a pipe nobody read and
     died. `desktop.session_env()` now takes the missing `SESSION_VARS` from the
     **systemd user manager**, which always has the real values — that also
     survives a reboot, which putting them in the unit file would not. Clipboard
     verbs worked all along only by luck: `wl-paste` falls back to the
     `wayland-0` socket when the variable is unset.
  2. **A successful launch used to hang.** `_run`'s `capture_output` waits for
     EOF, the app inherits those pipes and holds them open for its whole life —
     so a working launch blocked until the 10s timeout ("gtk-launch didn't
     respond") while a *failed* one returned instantly. GUI spawns go through
     `desktop.spawn_app()` with DEVNULL instead.
  3. **The app would die with the dispatcher.** A child inherits our cgroup, so
     `systemctl --user restart mission-dispatcher` (routine here) killed every
     app it had opened. `spawn_app` wraps the launcher in `systemd-run --user
     --scope --collect --slice=app.slice`, the same mechanism behind GNOME's
     `app-*.scope` units. Verified: firefox survives a restart.
- **`gtk-launch` exits 0 whether or not the app lived**, which is what let this
  hide. `_launch` therefore checks the process itself (`process_token()` → the
  Exec basename, or the desktop id for wrapper Execs like `flatpak run`) and
  reports "<app> didn't start" if nothing appears within `LAUNCH_SETTLE_S`. The
  match is against **argv[0] only** — matching the whole command line found a
  terminal that merely *mentioned* firefox and called it running (it fooled the
  first live test of the check). Verification is skipped when something matching
  was already running, since a second launch only raises the existing window.
- `dispatcher/spotify.py`'s client launch had bugs 1 and 3 too and now shares
  `spawn_app` (`wait_s=0` — `flatpak run` stays alive as the client's parent, so
  the MPRIS poll is what confirms startup).

## Operational notes (Claude auth — subscription only)

- **The Messages API backend was removed 2026-07-27** at the user's direction
  (they are not funding an API key). `dispatcher/quick.py` is now one backend:
  `claude -p --output-format stream-json` on the Claude Code **subscription
  login**. Gone with it: `HistoryStore`, `ApiAbort`, the pooled client, the
  unusable-key fallback/cooldown, `quick_backend` config, `resolve_backend`,
  `scripts/smoke_messages_api.py` (~240 lines + 29 tests). Recoverable from
  git at `ce93ea0` if that ever changes.
- **This supersedes half of locked decision #2** ("quick Q&A → streaming
  Messages API"). The decision's substance — quick streams, agentic runs
  headless — is unchanged; only the mechanism is. Recorded here rather than
  silently, because that decision is otherwise marked do-not-re-evaluate.
- **`runner.cli_env` stays, and matters more now.** The `claude` CLI *prefers*
  an API key over the claude.ai login when one is in the environment and says
  so ("claude.ai connectors are disabled because ANTHROPIC_API_KEY … takes
  precedence"). A stray key therefore **replaces** the subscription: an
  unfunded one breaks every run, a funded one silently bills each ~18k-token
  cold start to API credit. `cli_env` strips `ANTHROPIC_API_KEY`/
  `ANTHROPIC_AUTH_TOKEN` from **every** `claude` subprocess, quick and agentic.
  `mission-dispatcher.service` deliberately has **no** `EnvironmentFile`.
- The cost floor is therefore permanent: ~16-18k cache-creation tokens of
  Claude Code system prompt on every cold `claude -p`, ~$0.10-0.14 a question
  notional, TTFT ~3s. That is what makes `models.meta` (haiku for internal
  classification work) the one lever that actually moved the number.
- Costs logged on this path are **notional** — you are on a subscription, not
  metered billing. `Config.quick_cost()` and the `prices:` config block that
  priced Messages-API tokens by hand were **removed 2026-08-02** (dead since
  that backend went); every run reports its own `total_cost_usd`. The unread
  `models.classifier` key went with them — `dispatcher/classifier.py` is pure
  heuristic and has never called a model.
- **The repo no longer carries a `.env`.** It held a real `sk-ant-` key that
  nothing read (doctor.sh had been warning about it); moved out of the tree to
  `~/.local/share/mission-control/env.removed-from-repo-2026-08-02` (mode 600)
  on 2026-08-02. It was never committed — `git log --all -- .env` is empty and
  no `sk-ant-` string appears anywhere in history. The `.gitignore` entries stay
  as a guard. **Rotate that key** if it was ever real: it sat in a working tree.

## Operational notes (browser verification of the dashboard)

- The dashboard can be inspected headlessly with **chromium + CDP over Node's
  built-in WebSocket — zero deps** (`node --version` 22, `typeof WebSocket ===
  "function"`). Launch `chromium --headless=new --remote-debugging-port=9222
  --user-data-dir=…`, take the ws URL from `http://127.0.0.1:9222/json`.
  `Page.captureScreenshot` + `Emulation.setDeviceMetricsOverride` give a real
  look at phone widths. As session 6 noted, `--dump-dom` is useless here (the
  SSE `/events` stream never lets the page finish loading) — wait on explicit
  DOM conditions instead of load events.
- **Always `Network.setCacheDisabled` + a cache-busting query.** A rebuilt
  bundle gets a new hashed filename, but chromium caches `index.html` itself,
  so a re-measure after a CSS fix can silently re-test the OLD stylesheet —
  which is exactly what made a fixed overflow look unfixed on 2026-07-26.
- Found this way: the CommandBox `Send` button was **clipped at 390px** (the
  phone PWA width) ever since the session-19 mic button made that row too wide.
  Cause is the classic one — an `<input>` carries an intrinsic min-width from
  its size attribute, so a flex row won't shrink it and pushes the last button
  off-screen. `min-width: 0` on `.command-form input`, the same fix session 6
  applied to `.card`.

## Operational notes (dashboard "talk to Jarvis" button)

- The CommandBox widget (`ui/src/widgets/CommandBox.jsx`) has a mic button that
  is the **browser-side** twin of "hey jarvis": click to start, click again to
  stop → `getUserMedia`/`MediaRecorder` → `POST /stt` → `postTask` (mode auto,
  source **`ui-voice`**) streams the answer → spoken with the Web Speech API
  (`window.speechSynthesis` — there is **no server `/tts`**; a "mute reply"
  toggle suppresses it). The server mic (sounddevice) belongs to on-device
  jarvis only; a browser/phone can't use it, hence the browser records itself.
  Source is `ui-voice`, deliberately not the on-device `voice`, so their CLI
  sessions don't conflate. For phone use the mic needs a **secure context** —
  the Tailscale Serve URL is HTTPS (loopback is also secure). Caveat: iOS Safari
  can throttle `speechSynthesis` from async code — speaking is best-effort and
  never blocks the UI.

## Operational notes (computer-use T2/T3 — windows + browser tabs)

- **Neither tier needed its dependency.** The 2026-07-26 spike proved only
  the GNOME Shell route unreachable; session 24 measured AT-SPI answering,
  and T2 rides it over jeepney (`dispatcher/windows.py`: GetAddress →
  registry bus → GetChildren/GetRoleName/Name, Component.GrabFocus).
  T3 rides DevTools HTTP (`/json/list`, `/json/activate`) plus a ~150-line
  stdlib-socket WS client (`dispatcher/cdp.py`) for Runtime.evaluate.
  Q2 (dep approvals) is moot; `future/computer-use.md` §9 records it.
- **Qualifier discipline, both tiers.** Window shapes require a listing verb
  or open-state question beside "windows"; tab shapes require the word
  "tab"; focus/activate take anything but a bare generic. Live catches
  during wiring: `raise` is not a focus verb (volume collision), tab shapes
  run before window shapes, `tabs` is not an app name. Generative
  no-other-parser suites pin all three tiers pairwise.
- **Policy follows the data.** List verbs read local state (allow); focus
  and tab-switch move context (confirm); tab text lands in model context
  (confirm, quoted-data posture). Executors adapt WindowError/BrowserError
  to DesktopError, so misses settle failed and speak — the same honesty
  rule as T1.
- **Deliberately not built:** window close, click-by-name, page click/type.
  The evaluate seam is proven; the actions are not, and an unverified verb
  is the exact failure §5 of the plan doc exists to stop.
- **Browser needs its side:** nothing debuggable on
  `--remote-debugging-port` (default 9222, `browser.debug_port`) fails fast
  and speakably, never hanging the divert. `GET /desktop/verbs` lists the
  five new verbs with their policies.

## Known quirks / open items

- **Arch /dev/uinput quirk** (document in setup docs): the udev rule sometimes
  fails to apply and ydotoold won't start. Workaround: `chmod g+rw /dev/uinput`
  and re-`modprobe uinput`.
- **`jarvis/ptt_dictate.py`** — user's original PTT dictation script, saved
  verbatim 2026-07-05. Phase B: refactor its record+transcribe logic into
  engines behind the STTEngine ABC; keep the script alive as standalone
  dictation mode. Its evdev keyboard-watch loop is the reference for hotkey
  capture (needs user in `input` group).
