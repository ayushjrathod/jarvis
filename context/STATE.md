# STATE — read me first each session

_Last updated: 2026-07-18 (session 11, end)_

## Current phase

**Phases F AND G (v2 memory foundation + learning loop) IMPLEMENTED, both
acceptance-passing, live under systemd.** User waived the F review ("i am
not checking go ahead"), so G was built in the same session. Next: Phase H
(observability) per `context/v2-plan.md`, pending user go-ahead.

## Session 11 (cont. 2): Phase G — learning loop, live-verified

- **`dispatcher/reflection.py`** — Hermes-derived review prompt (signals,
  patch-before-create order, anti-capture rules; MIT, attributed) +
  `should_reflect()` policy gate (pure function, unit-tested).
- **Trigger**: agentic run 'done' with `num_turns >= 12` (config) →
  `_maybe_reflect` submits source="reflection" task with
  `resume_session_id` + `Edit(areas/**)` tools + $1 budget cap.
  `runner.build_cmd` grew `--resume` + per-task budget (extracted for
  testability). Meta-tasks (reflection/consolidate/learn) never re-reflect;
  reflections excluded from episode capture.
- **Live bug found & fixed**: the reflection prompt's own text matched the
  tasks area's triggers as a word subsequence → wrong SKILL context injected
  + telemetry pollution. `route()/submit()` grew `match_area=False`;
  regression test added.
- **`skill_usage` table** (hermes pattern): use bump on every area-tagged
  task, patch bump via mtime scan of areas/ after reflection/learn runs
  (per-tool events are Phase H). `GET /skills`, `POST /skills/curate`.
- **`dispatcher/curator.py`** — deterministic lifecycle only (their LLM pass
  deliberately skipped): active →(30d idle) stale →(90d more) archived =
  moved to `areas/.archive/` (registry can't see dotdirs; recoverable; no
  git ops — shows in git status). Protected: pinned, config list, timer
  areas. Monthly timer `mission-skill-curator` installed + enabled.
- **`areas/learn/`** + `POST /learn`: skill authoring with our frontmatter
  format (class-level names, subsequence triggers, least-privilege tools);
  empty request resumes the source's fresh quick session.
- **113 tests green (+16).** Live: a real 12-turn-threshold-lowered agentic
  run auto-spawned its reflection, which resumed the session for **$0.019 /
  1 turn** and correctly answered "Nothing to save."; curator live-run
  skipped all three protected areas; /learn 400s without material.

## Session 11 (cont.): Phase F built, tested, live-verified

All zero-new-dependency: SQLite FTS5 + stdlib + existing infra.

- **`dispatcher/db.py`** — new tables: `episodes` (+`episodes_fts`),
  `vault_files`, `entries` (+`entries_fts`), `entry_dates`; episode CRUD,
  hash-diff `replace_file_entries`, BM25 search with file/date filters;
  `fts_query()` quotes terms so user text can't hit FTS5 operators.
- **`dispatcher/ingest.py`** — heading-ancestry chunking (256-word budget,
  khoj pattern re-implemented — AGPL, no code lifted), ISO date extraction,
  mtime-prefilter + per-chunk MD5 diff incremental reindex.
- **`dispatcher/memory.py`** — letta-style bounded core blocks
  (`vault/memory/USER.md` 2000 / `MEMORY.md` 3200 chars) rendered into a
  `<memory_blocks>` prompt section; capture policy (skips cancelled,
  excluded sources, and the consolidator's own runs); consolidation export
  builder (episodes → `data/consolidation/<ts>.md`, gitignored).
- **`dispatcher/service.py`** — `_with_memory()` prepends blocks to quick +
  agentic context; `_capture_episode()` on both settle paths, best-effort.
- **`dispatcher/main.py`** — `/memory/search|reindex|blocks|consolidate`
  endpoints + startup reindex task. **`config.yaml`** — `dispatcher.memory`
  section (capture on by default per user's "what you think is right").
- **`areas/memory/`** — SKILL.md (voice: "remember that…"/"what do you
  remember…") + `agents/consolidate.md` (prompt discipline from letta
  sleeptime_v2 + mem0 additive extraction + Hermes memory guidance, with
  attribution). Seed blocks in `vault/memory/`.
- **systemd** — `mission-memory-consolidate.{service,timer}` 02:30 nightly,
  installed + enabled (next fire verified); `scripts/run_memory_consolidate.py`
  with the run_agent.py boot-race retry loop.
- **97 unit tests green (+24 in `tests/test_memory.py`).** Live-verified end
  to end: real quick task captured → `POST /memory/consolidate` ran the agent
  (sonnet, $0.14, 4 turns) → it wrote "favorite editor is Neovim" to USER.md
  with an absolute date and correctly judged trivia non-durable → fresh-source
  quick task answered "Neovim." from block injection alone (first recall test
  was invalidated by quick-session resume — caveat now in CLAUDE.md).

## Session 11 (2026-07-18): v2 planning + reference-repo research

User supplied a 14-layer v2 vision (modular brain, memory-as-most-important,
Hermes-style learning loop, skills, planner, personal OS, observability…)
and asked to plan it out with subagents cloning + auditing open-source repos.

- **Cloned into `references/` (gitignored)**: `hermes-agent` (NousResearch,
  MIT), `openjarvis` (Stanford, Apache-2.0), `mem0`, `graphiti`, `letta`
  (all Apache-2.0), `khoj` (**AGPL — patterns only, never lift code**).
- **Four subagent audit reports** saved under `context/research/2026-07-18-*.md`
  (memory-repos, hermes-agent, openjarvis, khoj). All four agents were killed
  once mid-run by the plan session limit (reset 5:50pm IST) and resumed
  staggered afterwards — worth remembering: don't run 4 parallel deep-read
  subagents on this subscription.
- **Deliverable**: `context/v2-plan.md` — gap analysis, research synthesis,
  and a phased roadmap:
  - **F** memory foundation (episodes table, letta-style core blocks in
    `vault/memory/`, khoj-pattern vault ingest + FTS5, `/memory/search`,
    nightly consolidation agent) — zero new deps;
  - **G** learning loop (Hermes review-prompt reflection fork via
    `claude -p --resume`, skill usage telemetry, `/learn`, deterministic
    curator);
  - **H** observability + graded brain (run_steps timeline, TTFT/ITL,
    `/stats`, X-ray footer, complexity tiers, self-improvement view);
  - **I** personal OS + NL→automations + notify-or-not gate;
  - **J** sqlite-vec + local ONNX embeddings + graphiti-style bi-temporal
    knowledge graph (dep approvals needed only here and I).
- Headline research findings: Hermes' learning loop is prompt-driven (their
  warm-cache review fork ≡ our `--resume` continuity, so reflection is cheap
  plumbing for us); memory needs no graph DB or cloud embeddings (letta runs
  sqlite-vec on SQLite in production; FTS5-only is a valid first step);
  OpenJarvis' parallel/hybrid execution doesn't transfer (GPU workers,
  per-token economics) but its observability package does; khoj's hash-diff
  ingest + heading-ancestry chunks are the Layer-11 spine.

## Open decisions for the user (v2)

1. ~~Approve/reorder phases~~ — user said "go ahead what you think is right";
   F→G→H→I→J kept, Phase F built. **Review Phase F before G starts.**
2. ~~Episode capture scope~~ — defaulted to capture-all (local-only data;
   `memory.capture` / `capture_exclude_sources` in config.yaml to change).
3. Dependency gates (sqlite-vec, onnxruntime/fastembed, rapidocr, pymupdf,
   spacy) — still open, only needed at Phases I/J.
4. ~~CLAUDE.md tracker/reuse-table updates~~ — done with the Phase F commit.

## Session 10: OpenClaw adoptions implemented (all 4 approved items live)

User approved session 9's proposals ("go ahead"). One commit each, all
tested, deployed, and live-verified — log: `context/sessions/2026-07-13-1.md`:

- **d64e0fa** — session-8 hardening committed first (was sitting uncommitted;
  needed so the per-item commits stay clean — still reviewable as one diff).
- **23d3c13** — sanitizer: snake_case survives to TTS (`backup_db.sh` no
  longer spoken as "backupdb.sh"); `_really_` emphasis still strips.
- **eb13aef** — quick-path continuity: follow-ups within
  `quick_session_idle_minutes` (config, 10) resume the source's previous CLI
  session via `claude -p --resume`; vanished session → one fresh retry.
  Live-verified twice (module + full HTTP path: codeword recalled; resumed
  turn ~6× cheaper via prompt cache, $0.0076 vs $0.048).
- **521eb8d** — usage-limit classification: model-scoped cap retries once on
  the fallback model (like refusal); plan-wide session cap fails fast with a
  spoken reason incl. parsed reset time (Jarvis now voices `speech` from
  done/failed events). Budget-cap errors explicitly excluded.
- **c771fdb** — timer tasks that die on a session limit requeue ~2min after
  the parsed reset (30min default, 6h cap, max 2 tries). In-memory: a
  dispatcher restart drops the pending retry (by design; journal + `requeued`
  SSE event record it).
- **2993047** — smoke_phase_a joins SSE deltas before asserting (grep-per-line
  missed answers split across delta events; surfaced by resume shifting token
  boundaries).

73 unit tests green (+8). smoke_phase_a 7/7, smoke_phase_b 12/12.
dispatcher+jarvis restarted on the new code; journals clean. Caveats:
continuity is CLI-backend only (messages_api would need its own history
store — noted in quick.py); `smoke-resume`/`api` sources now share context
within the idle window like any source does.

## Session 9: OpenClaw re-audit (approved session 10; all items implemented)

User asked what else `references/openclaw` offers. Clone refreshed to
a7b086df7 (2026-07-12, ~2.5k commits past the session-3 snapshot). Findings
appended to `context/adaptation-audit.md` ("OpenClaw-focused re-audit").
No code changed. Proposed, in rank order:

1. Quick-path conversation continuity — resume the CLI session
   (`claude -p --resume`, flag verified on 2.1.201) while fresh under an
   idle-minutes policy (their `reset-policy.ts`); session_id is already
   logged but unused, so every voice follow-up starts cold.
2. Usage-limit failure classification — our own runs table holds
   "session limit · resets 2:30pm" + 2× "Fable 5 limit" failures surfaced
   as generic "Sorry, that didn't work"; classify → model-scoped limit
   retries once on the fallback model, plan-wide limit speaks the reset
   time. Optional 2b: requeue timer tasks at reset time.
3. Sanitizer underscore fix (XS) — `_EMPHASIS` mangles snake_case
   (`backup_db.sh` → "backupdb.sh" spoken); lift their boundary guards.

Skipped with reasons (in the audit file): TTS directives, talk-mode
fast-context, steering/queued follow-ups, multi-provider failover; prior
skips re-affirmed.

## Session 8: fragility audit → hardening pass (implemented + verified)

Audit found, and the same session fixed, the whole robustness cluster —
details and file list in `context/sessions/2026-07-12-1.md`:

- **Startup reconciliation** of orphaned 'queued'/'running' rows (the
  07-09 daily-brief task had sat 'running' for 3 days) + `stream_quick`
  finally so client disconnects settle rows as 'cancelled'.
- **Root cause of the orphans found during deploy**: open /events SSE
  streams block uvicorn shutdown → systemd SIGKILL after 90s. Fixed with
  `timeout_graceful_shutdown=5`; dispatcher restarts now take ~5s.
- **run_agent.py retries connection failures 12×5s** (closes the "timer
  fires before dispatcher binds" open item from session 5) — reproduced
  the boot race live, watched it recover, and backfilled today's brief.
- **Voice service crash paths closed**: `httpx.StreamError` caught (it's
  a RuntimeError, not HTTPError — residual session-7 barge-in window);
  playback/TTS/mic errors contained per-interaction instead of killing
  the process; barge monitor got its own stop flag (stale-thread race).
- **Hotkey watcher supervised** (device unplug, late boot enumeration,
  callback errors) and **mission-dictate exits nonzero** on watcher death
  so Restart=on-failure actually fires.
- Quick CLI stream: 4MiB line limit + concurrent stderr drain. Queue
  watcher supervised. Empty SKILL.md triggers can no longer 500 /task.
  One bad vault file no longer breaks /vault/tasks. Near-miss wake band
  now scales with the threshold. Backup gets a sqlite busy timeout.

52 unit tests green (+6 new). All three services restarted on the new
code; smoke_phase_b 12/12. **Working tree uncommitted, awaiting user
review.** Untested-by-design: the messages_api quick backend (no API key
here) — verify it manually before exporting ANTHROPIC_API_KEY.

`mission-dispatcher.service`, `mission-jarvis.service` (now **wake-only**),
and the new `mission-dictate.service` are all **running and enabled**.

## Session 7: trigger split (wake=assistant, F9=dictation) + barge-in crash fix

User's desired UX: "hey jarvis" = hands-free assistant, hold-F9 = dictation
typed at the cursor. Changes:

- `mission-jarvis` → `--mode wake` (was `both`); F9 freed for dictation.
- New `systemd/mission-dictate.service` runs `jarvis.dictate` on F9; added to
  install.sh; enabled + running. **BLOCKED on ydotool: not installed** —
  transcription works but `ydotool type` fails until the user runs
  `sudo pacman -S ydotool` + enables its user service (the CLAUDE.md claim
  that ydotoold was already running was stale; corrected).
- **Crash fix**: barge-in → `brain.cancel()` → the dead SSE stream raised an
  unhandled httpx error inside `handle_utterance` and killed the whole
  service (observed 10:20 IST in the journal). Now caught
  (`except httpx.HTTPError`, jarvis/main.py). 44 tests green.
- "Wake word not working" diagnosis: it WAS working end-to-end — SQLite shows
  every test utterance answered ("Yep, I'm here…"). Replies play on the
  **default sink = JadeAudio JA11 earphones at 27% volume**; user likely
  wasn't wearing them or it was too quiet. A TTS→Player probe through the
  exact runtime path completed (ok=True). If still inaudible: check
  `wpctl status` default sink + `pactl get-sink-volume @DEFAULT_SINK@`.
- Post-test round 2: dictation confirmed working (ydotool installed by user).
  A 10:43 wake query failed on the **Claude subscription session limit**
  (recovered after the 2:30pm reset); a 15:00 wake fired but the user paused
  after "hey jarvis" (silent 5s window, by design). Added a **wake
  acknowledgment beep** (`wake_beep_ms: 120` in config.yaml, 0 disables;
  `play_beep_async` in jarvis/audio.py, fired from on_wake). 46 tests green.
  Then upgraded to **spoken acks** (`wake_ack_phrases` in config.yaml, random
  "Hmm?"/"Yes?"/"Mm-hmm?", Piper-pre-synthesized at startup; beep = fallback
  when the list is empty). Wake threshold 0.5 → **0.35** + near-miss score
  logging in the wake engine for data-driven tuning.

## Session 6 (cont.): agent-activity rows expand on click

`ui/src/widgets/AgentMonitor.jsx`: each row is now a toggle button — click
expands a detail panel (full text, task id, source, area, created, and
model/cost/error when the SSE event carried them). Verified by driving
chromium over CDP (node built-in WebSocket, no deps): expand/collapse works,
no horizontal overflow. Note: `chromium --dump-dom` is useless against the
dashboard — the SSE `/events` stream keeps the page "loading" forever.

## Session 6: dashboard horizontal-overflow CSS fix

Cards blew past the viewport on long content. Fixed in `ui/src/styles.css`:
grid columns → `minmax(0, 1fr)` + `min-width: 0` on `.card` (grid items
otherwise can't shrink below content width), and `overflow-wrap: anywhere`
on `pre.brief` / `.answer` so long URLs break. Rebuilt `ui/dist`; live
server confirmed serving the fix (no restart needed — StaticFiles).

## Session 5: model default moved INTO the dispatcher (sonnet / medium)

User wants sonnet at **medium** effort as the default *for this project*,
independent of whatever `~/.claude-per/settings.json` says (it had drifted
back to `claude-fable-5[1m]` / high since session 4; left as-is — it's the
user's interactive default, no longer load-bearing for the dispatcher):

- `config.yaml`: `models.agentic: claude-sonnet-5` (was null = CLI default),
  new `models.effort: medium` applied to every `claude -p` invocation.
- `dispatcher/runner.py` + `dispatcher/quick.py`: pass `--effort`; the quick
  CLI backend now also passes `--model` from `models.quick` (before, its
  first attempt sent no --model at all and silently ran the CLI default).
- Verified live: daily-brief ran on `claude-sonnet-5`, 11 turns, $0.53.
- 44 unit tests green.

**Found + recovered:** mission-daily-brief failed on 2026-07-10 09:29 —
timer fired before the dispatcher was listening (`After=` orders startup but
`scripts/run_agent.py` doesn't wait/retry on connection refused). Re-ran it
manually; `vault/briefs/2026-07-10.md` written. **Open item:** add a short
retry loop to run_agent.py (or `Restart=on-failure` + delay on the unit) so
a slow dispatcher bind at boot doesn't lose the day's brief.

## Session 4: default model → Sonnet 5 (high effort)

User asked to make Sonnet 5 (high effort) the default everywhere, not Opus or
Fable. Three separate Claude Code config dirs were in play — only two were
actually relevant:

- `~/.claude-per` (`CLAUDE_CONFIG_DIR` the dispatcher execs `claude` with,
  set in `config.yaml`) — **this is the one that matters for the project.**
  Was `claude-fable-5[1m]` / `xhigh`, now `claude-sonnet-5` / `high`.
- `~/.claude` (plain default, not referenced by the dispatcher or by this
  interactive session) — updated too (`opus` → `sonnet` / `high`) for
  consistency, though nothing in this repo depends on it.
- `~/.claude-dd` (this interactive Claude Code session's own config dir) —
  **left untouched**; it was already `sonnet`/`medium` beforehand and is
  unrelated to the dispatcher/mission-control project.

Also updated `config.yaml`: `dispatcher.models.quick` → `claude-sonnet-5`
(was `claude-fable-5`), added its pricing entry. `fallback: claude-opus-4-8`
deliberately left alone — that's the refusal-fallback model (locked decision
#3 in CLAUDE.md), not the daily driver, and orthogonal to this change.
Dispatcher was restarted once to pick up the change, verified healthy, then
stopped again per the user's request at session end.

## Session 3: reference-repo adaptation audit + 4 adopted improvements

Ran a systematic audit of `references/` (updated clones + 2 new: `agentic-os`,
`lifeos-template`) against our five phases — see `context/adaptation-audit.md`.
User approved and this session **implemented, tested, and committed** 4 items:

1. Queue watcher retries transient ingest failures (`queue_max_retries`)
   instead of failing on first error — `dispatcher/queue_watcher.py`.
2. Runner retries exactly once on a timeout/spawn-error whitelist only
   (never refusal/budget) — `dispatcher/runner.py`, `transient_retry_delay_s`.
3. `scripts/doctor.sh` — read-only health check replacing the manual
   "Known quirks" checklist.
4. Pre-wake-word ring buffer (`jarvis/wake_capture.py`, `wake_prebuffer_ms`
   config, default 400ms) — first word after "hey jarvis" no longer clipped;
   verified live via `scripts/smoke_phase_b.py`.

Item 5 (stream agentic runs for dashboard visibility) approved in principle
but **deferred** — scope as its own task when picked up. Items 6 (adaptive
endpointing) and 7 (decision/commitment journal life area) are **parked**,
do not build without explicit request.

## What runs where (verified live)

- `mission-dispatcher.service` — enabled + running; dashboard at
  http://127.0.0.1:8765/; crash auto-restart verified (kill -9 → new PID).
- Timers enabled: mission-daily-brief 07:30, mission-weekly-review Sun 18:00,
  mission-backup 03:30 (first backup already in data/backups/).
- `mission-jarvis.service` — installed, **deliberately not enabled** (user
  must be in `input` group + audio ready): `systemctl --user enable --now
  mission-jarvis`. Manual run still works (`python -m jarvis.main`).
- 31 unit tests green; smoke_phase_a.sh + smoke_phase_b.py both pass.

## What the user still needs to do

0. **`sudo pacman -S ydotool`** + `systemctl --user enable --now ydotool` →
   dictation-at-cursor starts working (mission-dictate already runs; only the
   typing step fails). If ydotoold won't start, see the /dev/uinput quirk.
1. `sudo usermod -aG input $USER` + re-login → then enable mission-jarvis.
   (Hotkey already works, so this is likely done — verify with `groups`.)
2. Reboot test (Phase E acceptance): after reboot,
   `journalctl --user -u mission-* -b` should show clean startups. Consider
   `loginctl enable-linger ayra` for boot-without-login.
3. Optional: Gmail MCP (`claude-per mcp add gmail …`) → email section appears
   in daily briefs automatically.
4. Optional: `ANTHROPIC_API_KEY` → quick path switches to low-latency
   Messages API (set it in mission-dispatcher.service env or shell).

## Known caveats (details in CLAUDE.md + session logs)

- Barge-in needs headphones or PipeWire echo-cancel.
- `Write(path)` allowedTools rules silently ignored — use `Edit(path/**)`.
- Area triggers match as in-order word subsequences (dispatcher/areas.py).
- Timers no-op (journal an error) if the dispatcher is down.

## Deferred (per spec, do not build unprompted)

Other life areas, cloud Routines, multi-agent fan-out, media area (mpv/yt-dlp),
Kokoro TTS swap.
