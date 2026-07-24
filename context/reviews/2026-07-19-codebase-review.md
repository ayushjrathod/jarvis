# Codebase review — 2026-07-19 (four parallel fresh-agent reviewers)

Read-only review of the whole tree after v2 (Phases F–J) shipped. Four agents,
one per slice: dispatcher core, memory/learning/automations, voice pipeline,
UI/tests/ops. Every finding was verified against source by the reviewing agent;
the three security-grade items were additionally reproduced against in-memory
DBs. **No fixes were applied.** Baseline: 192 unit tests green, hermetic.

Severity counts: 8 HIGH · 12 MEDIUM · ~18 LOW.

---

## Cross-cutting theme (the headline)

**Tool-scope and budget guardrails are convention, not enforcement.** Three
findings converge on the same hole from different directions:

- External request `metadata` can grant `Bash`/`Write` + raise the budget cap
  (dispatcher #1) — and granting Bash also strips the Layer-13
  `--disallowedTools Bash` net.
- Reflection runs can *write* `Bash` into an area's SKILL.md frontmatter
  (memory #1), which is the privilege manifest — a later routed task then runs
  unattended with Bash.
- Memory-block files (`vault/memory/*.md`, injected into every prompt) and the
  knowledge graph's `invalidated_ids` are both applied from LLM/untrusted
  content without a mechanical allowlist/cross-check (memory #2, #3).

Fixing the guardrail to be *mechanical* (allowlist intersection at the trust
boundary) closes dispatcher #1 and memory #1 together — the top priority.

---

## HIGH

### H1 — External `metadata` overrides tool allowlist + budget cap
`dispatcher/service.py:351-355,378-379` ← `dispatcher/main.py:33-38,120`
`POST /task {"metadata":{"allowed_tools":["Bash","Write"],"max_cost_usd":100}}`
is honored verbatim; also reachable via queue-file frontmatter. API has no auth.
Granting Bash removes the `--disallowedTools Bash` safety net.
**Fix:** treat `allowed_tools`/`max_cost_usd`/`resume_session_id` as trusted
only from internal server-set callers; strip/intersect for `api`/`queue`/`voice`.

### H2 — Reflection can rewrite the privilege manifest (SKILL.md frontmatter)
`dispatcher/reflection.py:16`, `areas.py:78`, `service.py:351-356`, `runner.py:117-122`
Reflection resumes a session that may contain untrusted vault/inbox content, with
`Edit(areas/**)` — which covers SKILL.md frontmatter. Injected text could add
Bash + a broad trigger to an area; next routed task runs it unattended.
**Fix:** validate frontmatter `allowed_tools` against a pinned allowlist at load
time (reject Bash/unscoped writes unless area is config-blessed); diff-check
tool grants after reflection/learn runs.

### H3 — One extraction reply can soft-wipe the knowledge graph
`dispatcher/graph.py:103-105`, `dispatcher/db.py:618-627`
`invalidated_ids` applied with no cap, no check against the ≤80 prompt
candidates; `isinstance(True,int)` means bools become fact ids. Verified live:
`[1..9999]` invalidates all facts; `[true]` invalidates fact 1. No un-invalidate
API. Same gap in `apply_reconciliation`; fact additions also uncapped.
**Fix:** intersect ids with the exact candidate set, cap per-batch, exclude bools.

### H4 — Barge-in cannot stop audio mid-sentence
`jarvis/audio.py:135-145`, `main.py:106-108`, `engines/tts_piper.py:48-52`
`Player.play` checks the interrupt flag only *between* chunks; piper yields one
chunk per sentence, so the check runs once before playback. A 300-char sentence
(~15-20s) is un-interruptible — violates locked decision #7's <200ms target.
**Fix:** slice each TTS chunk into ~100ms sub-frames, or callback-driven
`OutputStream` with `abort()` in `stop()`.

### H5 — Barge-in during a stream stall doesn't cancel the dispatcher call
`jarvis/main.py:98-121`
`speak_sentences` reacts to `barge` only when the queue yields; if barge fires
while parked on `get()` (brain stalled between sentences), nothing wakes it —
`busy` stays held until the next sentence or the 300s read timeout. Up to 5 min
unresponsive with the request still burning server-side.
**Fix:** race `sentence_queue.get()` against `barge.wait()` FIRST_COMPLETED, or
have `_barge_monitor` call `brain.cancel()` directly.

### H6 — Mic device dying mid-stream hangs the wake loop forever
`jarvis/wake_capture.py:41-49,77-85`, `audio.py:62-66`, `main.py:241-244`
Post-open device loss makes the PortAudio callback stop firing; `mic.read()`
returns `None` forever, both capture phases `continue`, no exception — so the
advertised "retry on mic loss" handler (open-time only) never runs. Wake word
permanently dead, survives replug, until process restart.
**Fix:** treat N consecutive `None` reads / inactive stream as device loss and
raise so the existing retry re-opens.

### H7 — Stale `_current_task_id` lets barge-in kill an unrelated agentic task
`jarvis/engines/brain_dispatcher.py:59,74-87`, `main.py:119-120,159-164`
Agentic branch sets `_current_task_id` and never clears it; `cancel()` (fired on
any barge-in, including over a spoken notice) POSTs `/task/{id}/cancel` which
kills any non-terminal task. Talking over an automation notice can kill a
long-running background agentic run.
**Fix:** clear `_current_task_id` in submit's `finally`; don't keep an agentic id
barge-cancellable once its ack finished.

### H8 — `setup_voice.sh` Piper fallback can half-apply then never self-repair
`scripts/setup_voice.sh:19-27`
Guards on the `.onnx` only but downloads two files under `set -e`; if the
`.onnx.json` curl fails, re-runs see the `.onnx` and skip the whole block —
Piper stays broken until manual deletion.
**Fix:** guard on both files, or download to temp + atomic move.

---

## MEDIUM

- **M1** `dispatcher/main.py:297-308` — no Origin/CORS check + unbounded raw-body
  `/stt`; a visited web page can `fetch()` a multi-GB blob to OOM, and widens the
  H1 delivery path. Fix: Content-Length cap + Origin/loopback assertion.
- **M2** `dispatcher/service.py:235` (quick), `main.py:125` — quick-path
  subprocess never registered in `self.procs`, so `POST /task/{id}/cancel` is
  cosmetic: returns 200 `cancelled` but the run keeps streaming and settles
  `done`, clobbering the state. (Client-disconnect barge-in still works.)
  Fix: register the quick subprocess and honor a cancelled flag before final status.
- **M3** `dispatcher/service.py` DB calls on the event loop vs. embedder thread —
  WAL write-lock collision freezes all SSE/quick/health for the write; >5s throws
  past handlers and strands a task `running`. Fix: DB writes via `to_thread`/writer
  queue + busy-retry.
- **M4** `dispatcher/memory.py:53-64` — `blocks_context` globs *all*
  `vault/memory/*.md` (not just USER/MEMORY), 2000 chars each, no file cap;
  consolidator can create new files there → persistent growing injection into
  every prompt, riding into `summarize` (unscoped Write). Fix: allowlist block
  files or cap total injected chars; scope `summarize` Write.
- **M5** `dispatcher/ingest.py:34,92,128` — junk filter splits on single spaces
  only; a one-URL-per-line `.txt` becomes one ">500-char word" and the whole file
  indexes as empty (verified). Defeats the Phase I bookmark-export use case; also
  drops CJK/base64 blocks. Fix: `body.split()` (all whitespace), re-check empty
  after filtering.
- **M6** `dispatcher/main.py:269-281`, `memory.py:88-93`, `notify.py:25-27` —
  episodes marked consolidated *before* the agent runs, and a failed
  consolidation is silenced by `NEVER_SURFACE_TASK_TYPES` even when `failed` →
  invisible data loss at 02:30. Fix: mark on 'done' only (or stamp export_path);
  let failed meta-tasks through the gate.
- **M7** `jarvis/engines/brain_dispatcher.py:56-60` — `submit()` branches on
  content-type, never checks status; a 500 JSON error body is spoken as "On it."
  (false confirmation). Fix: check `status_code`, yield speakable `error`.
- **M8** `jarvis/main.py:55,137,264-269`, `vad.py:49-62` — one shared `SileroVAD`
  mutated by wake-capture thread and barge-monitor thread; a notice spoken during
  phase-2 capture tramples VAD state (bad endpointing). Fix: separate VAD instance
  per consumer (state is 65KB).
- **M9** `jarvis/ask_screen.py:157-167` — only `take_screenshot()` is wrapped in
  `fail()`; `store`/`prune`/`launch_popup` (e.g. chromium missing) raise silently
  from a GUI shortcut with no terminal. Fix: extend the `fail()` wrapper +
  `shutil.which(chromium_bin)` precheck.
- **M10** `ui/src/api.js:33-41` — `postTask` never checks `resp.ok`; a dispatcher
  500 renders "undefined (task undefined)" and the ask popup hangs silent. Fix:
  check `resp.ok`, throw with status/detail.
- **M11** `ui/vite.config.js:13` — dev proxy omits `/automations` and `/stats`, so
  those widgets show "None yet" under `npm run dev` regardless of data (prod
  unaffected). Fix: add both prefixes.
- **M12** `ui/src/AskScreen.jsx:29-30,81` — `patchLast` patches by last index; a
  mic error landing mid-stream steals the in-flight answer's deltas into the wrong
  bubble. Fix: capture target index/id at submit.

---

## LOW

- **L1** `dispatcher/db.py:599-616` — `add_fact` spans multiple connections
  non-atomically; a mid-way lock failure leaves a searchable fact with null/partial
  entity links. Fix: single transaction.
- **L2** `dispatcher/runner.py:189`, `quick.py:140,183-185` — orphaned stderr
  reader task on success paths; timeout branch `kill()`s without `await wait()`.
  Fix: await/cancel in `finally`, wait after kill.
- **L3** `dispatcher/embeddings.py:59-72`, `db.py:757-779` — backfill/reindex race
  inserts vec rows for deleted ids (AUTOINCREMENT prevents corruption, but orphans
  accumulate, KNN recall decays, nothing vacuums). Fix: conditional insert in one
  txn + orphan sweep.
- **L4** `dispatcher/automations.py:70,130,144,158-167` — fixed-offset local tz;
  daily/weekly/once schedules crossing a DST boundary fire an hour off (verified for
  a NY host; not reachable on IST). Fix: resolve zone via `zoneinfo`.
- **L5** `dispatcher/automations.py:32-41,64-66` — `nl_detect` hijacks
  "remember that I ... every morning" into a daily automation instead of a memory
  write. Fix: veto divert when an area trigger matches, or require an imperative
  schedule verb.
- **L6** `dispatcher/ingest.py:315-316,353-356` — a briefly-absent `index_dir`
  purges its whole index (re-chunk + re-embed churn, no permanent loss). Fix: skip
  stale-path deletion for dirs absent this walk.
- **L7** `jarvis/engines/brain_dispatcher.py:94-119` — `notices()` reconnects with
  no backoff/status check on a cleanly-ended stream (404 from old dispatcher → tight
  loop). Fix: `raise_for_status()` + unconditional sleep.
- **L8** `jarvis/engines/brain_dispatcher.py:100-107` — a scalar SSE `data` payload
  (`null`/`42`) raises `AttributeError` outside the catch → kills `notices_loop` →
  process exits. Fix: normalize non-dict payloads to `{}`.
- **L9** `jarvis/dictate.py:23-31`, `ptt_dictate.py:82-83` — ydotool types
  transcripts verbatim; a hallucinated `\n` becomes Enter in the focused window
  (executes a terminal line). Plus one daemon thread per key-release, no
  serialization. Fix: strip control chars, single worker queue.
- **L10** `jarvis/ask_screen.py:84-95` — a D-Bus *error reply* from Screenshot
  isn't detected; error text is used as an object path → misleading
  `MatchRuleInvalid` dialog. Fix: check `reply.header.message_type` first.
- **L11** `jarvis/audio.py:26-38`, `main.py:209-215` — PTT recorder has no duration
  cap; a wedged F9 grows ~115MB/hour then hands whisper a multi-hour clip. Fix: cap
  `_frames` at a max duration.
- **L12** `ui/src/widgets/AgentMonitor.jsx:56-60,77` — expanded step timeline
  fetched once and frozen; live `step` events only shown while collapsed. Fix:
  refetch on expand when non-terminal / append events for open rows.
- **L13** `tests/test_jarvis.py:96-104` — asserts live config.yaml values, so a
  supported one-line engine swap fails the suite. Fix: fixture config in tempdir.
- **L14** `tests/test_personal_os.py:101-122` — optional-dep tests `import pymupdf`/
  `cv2` in the body → ERROR (not SKIP) without the deps. Fix: `skipUnless`.
- **L15** `scripts/doctor.sh:90` — timer health check covers 3 of 7 timers (misses
  consolidate/reconcile/curator). Fix: derive list from `systemd/mission-*.timer`.
- **L16** `ui/src/widgets/{Automations,Tasks}.jsx` — mutation handlers have no
  error handling; a 404 leaves stale UI, unhandled rejection. Fix: catch + refresh.
- **L17** `scripts/setup_ask_screen.sh:30-31` — `$PWD` interpolated unquoted into
  the gsettings command; breaks on spaces, goes stale if repo moves. Fix: quote +
  note re-run after move.

---

## Verified sound (checked, no finding)

- No `dangerouslySetInnerHTML`/`innerHTML` anywhere in ui/src — all LLM text
  renders as escaped React nodes. SSE subscription closed on unmount; committed
  ui/dist matches src.
- Meta-task exclusion lists airtight: no gate-gates-gate, no reflection loop, NL
  divert only in `POST /task`. Scheduler edges hold (advance-before-submit,
  catch-up, once→NULL, re-enable recompute). AUTOINCREMENT makes rowid-reuse
  corruption impossible. FTS5 operator injection blocked by per-term quoting.
- Quick tasks without granted tools still get `--disallowedTools Bash`.
- `resolve_screenshot`/`toggle_task` traversal guards correct; subprocess argv has
  no shell. Portal predicted-path subscription registered before the call (can't
  miss the signal); all waits bounded by timeout=300. evdev never `grab()`s.
  Sample-rate/dtype chain (16k int16 → f32) consistent; Silero 64-context +
  openwakeword mel-flush workarounds implemented as documented.

---

## Recommended fix order

1. **H1 + H2 together** — one mechanical allowlist at the trust boundary (external
   metadata + SKILL.md frontmatter validated against config). Closes the biggest hole.
2. **H3** — cap + candidate-intersect graph invalidation (a few lines; prevents
   silent graph wipe).
3. **M5** — chunker `split()` fix (one word; users hit this organically).
4. **H6** — mic-loss detection (reliability of the always-on wake service).
5. **H4/H5/H7** — barge-in hardening (the signature feature; more involved).
6. **M6, M2, M1, H8, M10** — silent-failure and error-swallowing fixes.
7. LOW items as convenient.
