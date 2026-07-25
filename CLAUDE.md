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

## Phase tracker

| Phase | Scope | Status |
|---|---|---|
| A | Dispatcher core (API, queue watcher, classifier, headless runner, refusal fallback, SQLite logging, hooks) | **implemented, acceptance passing — awaiting user review** |
| B | Jarvis voice pipeline on the dispatcher | **done — user-verified on hardware 2026-07-06** |
| C | Tasks/work vertical slice + daily brief + weekly review | **implemented, acceptance passing — Gmail MCP left to user OAuth** |
| D | React dashboard | **implemented — served by dispatcher at :8765, headless-render verified** |
| E | systemd wrap-up | **implemented — units live, auto-restart verified; reboot test = user** |
| F | v2 memory foundation (episode capture, core blocks, vault FTS index, /memory API, nightly consolidation) | **implemented, acceptance passing** (user waived review) |
| G | v2 learning loop (reflection fork on complex runs, skill telemetry, /learn + learn area, deterministic curator) | **implemented, acceptance passing** (user waived review) |
| H | v2 observability (run-step timeline + live SSE steps, TTFT/ITL latency, /stats, dashboard X-ray + stats widget) | **implemented, acceptance passing — awaiting user review** |
| I | v2 personal OS (txt/html/pdf/image ingest + vault/inbox, NL→standing automations, notify-or-not gate) | **implemented incl. PDF/OCR, acceptance passing** (review waived) |
| J | v2 semantic + graph memory (sqlite-vec hybrid search, bge-small local embeddings, bi-temporal fact graph, weekly reconcile) | **implemented, acceptance passing** (review waived) |
| — | Ask-about-my-screen (`<Super><Alt>a` → portal shot → popup → streamed answer) | **implemented, smoke passing — user E2E pending** (run scripts/setup_ask_screen.sh) |
| — | Spotify media control ("hey jarvis, play …" → MPRIS, deterministic) | **implemented, 332 tests green — user E2E pending** (run scripts/setup_spotify.sh) |
| — | Phone access via Tailscale + installable PWA dashboard | **implemented, 334 tests green — user setup pending** (run scripts/setup_tailscale.sh) |

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
2. **Quick vs agentic routing** — quick Q&A → streaming Messages API; agentic →
   `claude -p` headless with per-task-type `--allowedTools`, `--max-turns`, spend
   cap. Prefer read-only scopes; never blanket permission-skipping unattended.
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
| open-jarvis/OpenJarvis (Apache-2.0) | observability: TTFT/ITL stats, run_steps timeline, X-ray footer (Phase H) |
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
- **No API credentials on this machine** (subscription OAuth only): quick path
  auto-falls back from Messages API to `claude -p` stream-json
  (`quick_backend: auto`). Adding `ANTHROPIC_API_KEY` flips it to the
  low-latency Messages API path with server-side refusal fallbacks.

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
  ~1.1s; TTS starts instantly. Quick-path first delta ~3-5s on the CLI
  backend (Messages API path will cut this substantially).
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
  resumes the run's CLI session (`--resume`, warm cache — measured $0.019)
  with `Edit(areas/**)`-scoped tools and the Hermes-derived review prompt in
  `dispatcher/reflection.py`. "Nothing to save." is a normal outcome.
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
  as the quick path) — that's what makes reflection nearly free.

## Operational notes (learned Phase H)

- Agentic runs now use `--output-format stream-json --verbose`: every message
  becomes a `run_steps` row (init/text/tool_use/tool_result/result, clipped
  summaries, elapsed_ms) and a live SSE `step` event — the dashboard shows
  the current step under running rows and the full timeline on expand
  (`GET /task/{id}/steps`). The result object carries the same fields the
  old json format did.
- Quick runs record `ttft_ms` / `itl_p95_ms` / `tokens_per_s` (additive
  `runs` columns via the idempotent MIGRATIONS loop in db.py). First real
  number: CLI backend TTFT ≈ 2.6s — the Messages API comparison is now
  measurable, not anecdotal.
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
  past-due row can't instant-fire. Empty/absent `automations:` config block
  disables the whole feature (that's what keeps unit tests LLM-free).
- **Notify gate**: 'done' automation/timer results run a `notify-gate` quick
  task that **resumes the settled run's own session** (measured $0.009 —
  cheaper than reflection) → `NOTIFY: <summary>` fires an SSE `notify` event
  (with `speech`) + notify-send; `SKIP:` is logged + `notify_skipped` event
  only. Fail-open: gate breakage notifies with a generic summary. done/failed
  events suppressed by policy carry `surface: false`; the jarvis brain
  honors both — **reflection/consolidation completions are no longer spoken**
  (before Phase I every agentic 'done' was announced when jarvis was up).
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
  land in well under a second. `POST /task` diverts on it (mode=auto only)
  **after** the automation divert, so "every morning play jazz" still becomes
  a standing automation. Non-music text returns None and routes as before;
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

- **Trust boundary (H1/H2)**: metadata from an external source
  (api/queue/voice/ui/screen) can NARROW tools/budget but never WIDEN them —
  `sanitize_untrusted_metadata` drops `allowed_tools`/`resume_session_id` and
  clamps `max_cost_usd` to the cap; only server-internal spawns pass
  `trusted=True`. Areas strip Bash/unscoped Edit-Write from SKILL.md
  frontmatter at load unless listed in `security.privileged_areas` (empty by
  default) — a reflection/learn run editing `areas/**` can't grant itself Bash.
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
  `failed`; a tolerable denial (e.g. Gmail MCP absent, the brief writes anyway
  and skips its email section by design) → `done` with the denial recorded in
  `denied_tools` and logged as a warning.
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

## Known quirks / open items

- **Arch /dev/uinput quirk** (document in setup docs): the udev rule sometimes
  fails to apply and ydotoold won't start. Workaround: `chmod g+rw /dev/uinput`
  and re-`modprobe uinput`.
- **`jarvis/ptt_dictate.py`** — user's original PTT dictation script, saved
  verbatim 2026-07-05. Phase B: refactor its record+transcribe logic into
  engines behind the STTEngine ABC; keep the script alive as standalone
  dictation mode. Its evdev keyboard-watch loop is the reference for hotkey
  capture (needs user in `input` group).
