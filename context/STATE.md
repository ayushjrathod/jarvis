# STATE — read me first each session

_Last updated: 2026-07-27 (session 21, end)_

## Session 21 (2026-07-27): Messages API backend made production-ready

User opened `.env` (an `ANTHROPIC_API_KEY` was already sitting in it) and asked
for next steps. **Nothing was reading that file** — no `EnvironmentFile` on the
unit, the variable absent from the running dispatcher, backend still
`claude_cli`. Full detail: `context/sessions/2026-07-27-1.md`. **439 tests
green** (411 → 439). Commit `ce93ea0`.

- **The API backend had never executed here**, and was missing three things the
  CLI path provides — switching would have silently dropped all three:
  **memory blocks** (`_stream_api` didn't even take `context`; locked decision
  #4), **continuity** (now `quick.HistoryStore`, which mints a `session_id` so
  `quick_sessions`/`resume_session_id` work unchanged on both backends), and
  **barge-in** (`quick.ApiAbort` duck-types what `Service.cancel` touches;
  decision #7). Client is now pooled per loop — a fresh TLS handshake per voice
  question was ~300ms of TTFT.
- **Measured:** $0.00051/question vs **$0.14052** on the CLI (**275x** — the
  CLI pays ~16-18k system-prompt tokens per cold run), TTFT ~1.5-1.8s vs
  ~3.2-3.8s (~2x). The earlier "sub-second" prediction was **wrong**.
- **Two traps, both found by testing with the real key, both fixed in code:**
  the key **has no credit**, and `auto` routes to the API the moment the env
  var exists — so an unusable key would 400 every question (now falls through
  to the CLI + a 15-min cooldown); and the **`claude` CLI prefers the key over
  the claude.ai login**, so an exported key *replaces* the subscription
  (`runner.cli_env` now strips it from every `claude` subprocess).
- **Wired in but pinned:** the unit loads `.env` via `EnvironmentFile=-`, and
  **`quick_backend: claude_cli`** deliberately. → **Flip that one line to
  `auto` once the account has credit**, restart, and verify with
  `scripts/smoke_messages_api.py`. Until then nothing changes and nothing
  breaks.
- **Agentic path verified before the overnight timers** (`cli_env` is on that
  path too): real agentic run at 01:40 → `done`, correct output, $0.2587 =
  the subscription path. The 02:30/03:30/07:30 timers are safe.

**Docs audit (`35cf466`) — every doc now matches the code.** README was badly
stale (dashboard "not built yet", "31 unit tests", **F9 documented as
push-to-talk** when it's been dictation since session 15); the in-app
`/system-docs` page had the same wrong trigger layout plus a notify-gate
paragraph describing the resume removed the day before; `vault/inbox/README.md`
predated the watcher; the feature-gap doc's open decisions are resolved.
**All 32 dispatcher routes verified documented** (diffed mechanically).
`scripts/doctor.sh` gained checks for desktop binaries, the quick-path backend
(unfunded key, `.env` gitignored) and Tailscale.

- **That last check immediately found a real bug**: `mission-tailscale-serve`
  had been **dead for a day** — at boot it ran while tailscaled was still in
  `NoState` and exited 1, and a `oneshot` with no restart stays down.
  `After=tailscaled.service` orders against the unit *starting*, not the daemon
  being *ready* (same shape as the session-8 boot race). Fixed with an
  `ExecStartPre` readiness wait. Phone access never actually broke — `serve
  --bg` persists the mapping in tailscaled's own state — so the exposure was a
  fresh boot after a state reset.
- **Overnight timers verified on the new haiku routing** (fired as catch-up at
  12:59 when the box woke): brief written, consolidation updated MEMORY.md, and
  the **notify gate ran on haiku at $0.0493** vs $0.126–0.189 before. Session
  20's cost fix is confirmed working unattended.

## Session 20 (2026-07-26): cost fix · computer-use T1 · degraded mode · inbox watcher

User asked "what's next" and chose **all four** offered directions. All four
landed, each committed separately. **407 tests green** (334 → 407).
Full detail: `context/sessions/2026-07-26-1.md`.

**Session-start finding — the session-17 fix held.** The weekly review that
session 17 predicted would break on Sunday 07-26 wrote correctly
(`vault/briefs/week-2026-07-26.md`, fired 22:49 as a catch-up), as did both
daily briefs. The nightly consolidator also **corrected its own bad memory**
(rewrote the stale "Write gate blocks everything" MEMORY.md entry). Phone is
on the tailnet; Spotify creds and the ask-screen keybinding are in place.

1. **`7ba934b` meta-task cost.** CLAUDE.md's "warm resume is nearly free"
   claim was wrong: only the first notify-gate run ever cost $0.009, the 11
   since averaged **$0.141**. Measured the causes — resuming a long transcript
   for a one-line verdict ($0.141 vs $0.103 fresh), and the ~16k-token Claude
   Code system prompt on every cold `claude -p` making the model rate dominate
   ($0.103 sonnet vs $0.028 haiku). Fixed both: gate no longer resumes, and
   `models.meta`/`models.meta_task_types` route notify-gate, automation-parse
   and media-parse to haiku. `graph-extract` excluded on purpose (its output is
   graph fact text). Live: **$0.0405 vs $0.189** for the previous real run.
2. **`90d90dc` computer-use T1.** Spike first — and it **disproved the design
   doc**: `Shell.Eval` is locked and `Shell.Introspect` returns AccessDenied,
   so window management needs a shell extension; brightness needs
   brightnessctl. Both deferred. Shipped `dispatcher/desktop.py` zero-dep
   (status/volume/mute/lock/launch/open/clipboard) with the §5 safety plane:
   allow/confirm/deny per verb failing closed, `open` scheme-restricted at the
   executor (**a test caught `javascript:` passing the guard** — urlparse is
   the wrong tool for opaque schemes), SSE `confirm` + dashboard ConfirmBar +
   **voice "yeah"/"nope"** answering.
3. **`aee97a6` degraded mode.** The plan cap no longer kills the assistant:
   a rate-limited quick question is answered **extractively** from the local
   hybrid index. Live testing found the load-bearing detail — KNN always
   returns something, so a BM25 relevance anchor is required or unrelated
   chunks get quoted as answers.
4. **`56d41bb` inbox watcher.** First non-clock trigger: a file landing in
   `vault/inbox/` is indexed and acknowledged automatically (two-phase settle,
   seeded first pass, removals reindex without notifying).

**Follow-up (`7100ffe`) — closed the three loose ends session 20 flagged:**
- Removed the 6 **fabricated** task+episode rows my degraded-mode testing left
  behind (synthetic session-limit failures). They would have been fed to
  tonight's 02:30 consolidation as real events. Success rate 86.5% → 97.8%;
  orphan vectors swept; DB backed up first, and the delete asserted every row
  carried the synthetic error marker before touching anything.
- **Browser pass via chromium + CDP** (Node built-in WebSocket, zero deps).
  ConfirmBar verified for real: renders above the grid, Yes/No work, click →
  executes → resolved row → nothing left parked, wraps fine at 390px. But it
  exposed a **pre-existing bug**: the CommandBox `Send` button was clipped at
  phone width, and had been since session 19's mic button. Fixed with
  `min-width: 0` on the input. **Cache-disable when re-measuring** — the first
  re-check silently re-tested the cached old stylesheet.
- **`lock` verb verified** — and next to it, `_status` could *never* report
  lock state: a systemd **user** service isn't in a login session, so
  `loginctl show-session self` always failed and the error was swallowed. Now
  reads the session bus (`ScreenSaver.GetActive`), with None for "couldn't
  tell". 411 tests green.
- Also gitignored `.env` — an untracked secrets file was sitting where a
  `git add -A` would have committed it.

**Not built, deliberately: the approval queue.** The surface now exists and is
reusable, but nothing outbound exists to gate (Gmail is read-only and not
OAuthed). Wire it to the first real outbound action rather than building it
speculatively.

**User-side, unchanged:** Gmail OAuth · reboot test (still never run) · phone
add-to-home-screen · ask-screen/media E2E · dashboard visual pass (the new
ConfirmBar has not been seen in a browser).

## Session 19 (2026-07-24): "talk to Jarvis" voice button on the dashboard

Follow-on to session 18. New mic button on the **main dashboard** (CommandBox
widget) = the same loop as "hey jarvis", browser-side: click to start listening,
click again to stop → `getUserMedia`/`MediaRecorder` → `POST /stt` → `postTask`
(mode auto, source **`ui-voice`**) streams the answer into the widget → speaks it
with the Web Speech API (`speechSynthesis`; there is no server `/tts`). For phone
use over the Tailscale HTTPS URL (mic needs a secure context — Serve is HTTPS).

- **UI only, no Python/API change** (`/stt` + `/task` already exist + proxied).
  `jarvis/*.py` untouched. Changed: `ui/src/widgets/CommandBox.jsx` (mic toggle +
  shared `runTask` + `say()` + "mute reply" toggle) and `ui/src/styles.css`
  (dashboard-scoped `.command-form button.mic` styles, tap-sized).
- **334 tests green** (UI-only; build is the bar). `ui/dist` rebuilt —
  **JS `index-DZvyj-0c.js`, CSS `index-BKjwyAup.css`** (CSS changed this time).
  Running dispatcher already serves it (StaticFiles, no restart).
- **Committed** with session 18 as one batch — `84e1290` feat: phone access via
  Tailscale + installable PWA + dashboard voice button. Dist assets force-added
  past the ignore; old baseline `index-CjFcS_Jm.js`/`index-qeg-kXaw.css` deleted.
  **Working tree clean.**
- **User-side**: grant mic permission; caveat — iOS Safari can throttle
  `speechSynthesis` from async code (best-effort, never blocks). Details:
  `context/sessions/2026-07-24-3.md`.

## Session 18 (2026-07-24): phone access via Tailscale + installable PWA

Implemented the phone-access work from `future/phone-access-and-feature-gaps.md`.
User picked **Tailscale** (over LAN-only/Cloudflare/ngrok) and **yes** to the PWA.

- **Origin guard now trusts a configurable front-door host.** The guard
  (`dispatcher/main.py` `_origin_is_local`/`guard_origin`) is CSRF-only, not
  auth; the SPA loaded over `https://<box>.<tailnet>.ts.net` POSTs with that
  Origin and would 403. New `security.public_hosts` (config.yaml, str-or-list) →
  `Config.public_hosts` → fed into the guard beside loopback + `cfg.host`. It's
  ONLY the origin allowlist; Tailscale is the access control (dispatcher stays on
  127.0.0.1, Serve fronts HTTPS — no token middleware, no public port). ngrok was
  rejected in the doc precisely because it *would* force bearer-token middleware.
- **Serve unit + setup script** (both user-side, can't test here):
  `systemd/mission-tailscale-serve.service` (oneshot `tailscale serve --bg
  --https=443 http://127.0.0.1:8765`); `scripts/setup_tailscale.sh` idempotently
  brings up tailscaled, sets the user as operator, installs+enables the unit, and
  **prints the exact `public_hosts` line** for the box's MagicDNS name.
- **PWA** (shipped, live-verified served): `ui/public/{manifest.webmanifest,
  sw.js}` + PIL-generated icons (192/512/maskable/apple-touch/favicon, dark "MC"
  tile); `ui/index.html` links them, `ui/src/main.jsx` registers the worker.
  Worker is minimal on purpose (live SSE dashboard) — installable + shell/asset
  cache only, never `/events` or POSTs. `mimetypes.add_type(".webmanifest")` in
  main.py is defensive (this Python's stdlib already knows it). `ui/dist` rebuilt
  (new JS hash `index-BsED9CnX.js`; CSS unchanged `index-qeg-kXaw.css`).
- **334 tests green** (+2 origin-guard: allowlisted host passes, other
  cross-origin still 403s). Dispatcher restarted on the new code; live-checked
  `/manifest.webmanifest` → 200 `application/manifest+json`, `/sw.js`, all icons
  200, SPA index carries the manifest link.
- **Committed** as part of `84e1290` (batched with session 19 — see that block).
  Note: session 18's interim `index-BsED9CnX.js` was superseded by session 19's
  rebuild; the committed dist assets are `index-DZvyj-0c.js`/`index-BKjwyAup.css`.
- **User-side**: `sudo pacman -S tailscale` → `scripts/setup_tailscale.sh` →
  paste the printed `public_hosts` line → restart dispatcher → Tailscale app on
  the phone → open the URL, add to home screen. Details:
  `context/sessions/2026-07-24-2.md`.
- **LIVE (user ran setup, same session):** tailnet name
  `archlinux.tail79c6ce.ts.net`; `config.yaml public_hosts` set to it; user
  enabled HTTPS Serve in the admin console; `mission-tailscale-serve` active +
  enabled. Verified from the box: `https://archlinux.tail79c6ce.ts.net/health`,
  `/`, and `/manifest.webmanifest` all 200 over tailnet HTTPS. Only the phone
  join + add-to-home-screen remains.

## Session 17 (2026-07-24): daily-brief regression fixed (timer trust boundary)

Found live on session start: the daily brief wrote **nothing** on 07-21/22/23
(3 runs, all falsely `done`, ~$0.92 wasted); weekly review would break the same
way Sun 07-26. **Cause: session-13's own H1 trust boundary.**
`sanitize_untrusted_metadata` strips `allowed_tools` off external requests, but
`scripts/run_agent.py` (every timer's submit path) sent the brief's
`Edit(vault/briefs/**)` grant in exactly that key → dropped → agent fell back
to read-only → every Write denied. A denied-every-write run *still* reported
`success` (CLI surfaces a missing grant as an ordinary is_error tool_result),
which is why it hid 3 days.

**Fix (2 parts, live-verified):** (1) the dispatcher now resolves an agent's
tools **from disk** (`AreaRegistry.agent_tools`, slug-guarded, same sanitizer as
SKILL.md); `run_agent.py` sends `metadata.agent` (a reference), not the grant —
restores the tools without reopening H1. (2) `runner.py` flags permission
denials; a denial that stopped the run producing its **declared output file**
(mtime vs pre-spawn `wall_t0`) → `failed`; a tolerable denial (Gmail MCP absent,
brief written anyway) → `done` + `denied_tools` recorded + warning logged.
**332 tests green** (+12). Live: fresh brief `8ca3f9b4bdc9` → `done`, file
written, Gmail denial tolerated. Details: `context/sessions/2026-07-24-1.md`.

**Committed** (user reviewed the batch, chose two commits): `672a994` harden —
session-13 review remediation (session-13-only files); `af693b2` feat —
sessions 14–17 (ask-screen finalize, media control, docs page, PTT trigger, the
brief fix) + the shared dispatcher files session 13 also touched. File-level
split (hunk staging unavailable here), so only HEAD is verified green (332
tests); `ui/dist` assets force-added past the ignore; `data/spotify.json` stays
gitignored; CLAUDE.md folded the deferred hardening + timer-trust notes.
**Working tree clean.** Only user-side items remain (Gmail OAuth, reboot test,
ask-screen/media E2E, dashboard visual pass).

## Session 16 (2026-07-23): Spotify media control ("hey jarvis, play …")

Music commands now bypass Claude entirely: `dispatcher/spotify.py` parses
"play X" / "pause" / "skip" / "what's playing" / "volume 40" deterministically
and drives the local Spotify desktop client over **MPRIS** (jeepney — no new
deps). Song lookup is one **client-credentials** Web API search (app-only auth,
**no Premium needed**; researched this session — Premium Student *is* full
Premium, so the user-OAuth path stays open if playlists/liked songs are wanted
later). Vague-but-musical text ("put on something chill") costs one
`media-parse` quick call that picks **search words only** — the action stays
deterministic, so no area and no Bash/D-Bus reach for the model.
`POST /task` diverts on it (mode=auto, after the automations divert), plus
`POST /media` and `GET /media/state`. **320 tests green** (263 → 320);
dispatcher restarted and the divert verified live with no Claude run; normal
routing unaffected. **Uncommitted**, like sessions 13-15.
**User-side: run `bash scripts/setup_spotify.sh`** (free app registration at
developer.spotify.com) — until then `play <song>` says so, transport works.
Details: `context/sessions/2026-07-23-1.md`.

## Current phase

**The entire v2 plan (Phases F–J) is implemented, acceptance-passing, live
under systemd** — user approved + installed the deps 2026-07-19 (sqlite-vec,
fastembed, pymupdf, rapidocr, jeepney) and waived phase reviews. Also
shipped: ask-about-my-screen (ssplan.md) and the Layer-13 Bash hardening.
**Session 13 ran a full codebase review + remediation** — 8 HIGH / 12 MED /
~18 LOW findings triaged into 7 work units, all applied (261 tests green,
uncommitted, awaiting user review). **Only user-side items remain** (ask-screen
manual E2E after setup_ask_screen.sh, Gmail OAuth, reboot test, dashboard
visual pass). Deliberately unbuilt: H4 complexity tiers, media area, Kokoro
swap, multi-agent fan-out (all "do not build unprompted").

## Session 15 (2026-07-22): hold-to-talk assistant on Right Ctrl

The assistant now has a second trigger next to "hey jarvis": **hold
`jarvis.ptt_key` (KEY_RIGHTCTRL) → listen → release → ask**, same
STT/dispatcher/streamed-TTS path, no VAD endpointing (the key edge is the
endpoint). New config `ptt_key` + `ptt_beep_ms` (90ms press/release beeps);
`ptt_key` is deliberately separate from `trigger_key` (F9 = mission-dictate)
because both services watch raw evdev — main.py warns if they're equal.
Pressing PTT mid-reply barges in (stops playback → cancels the call).
`mission-jarvis.service` switched `--mode wake` → `--mode both`, reinstalled
and restarted (journal shows "watching 4 keyboard(s) for 97"). 263 tests
green. **Uncommitted**, like sessions 13/14. User-side check: hold Right Ctrl,
speak, release — the reply should be spoken.

## Session 14 (2026-07-22): docs page at /system-docs

In-app documentation now ships with the dashboard: **http://127.0.0.1:8765/system-docs**
(linked from the dashboard header) — user guide + HTTP API reference for all
26 routes, rendered by `ui/src/Docs.jsx` from data in `ui/src/docs/content.js`.
Path is `/system-docs` on purpose: FastAPI's Swagger UI keeps `/docs` (user's
call), and a test now guards that. `dispatcher/main.py` grew a shared
`_spa_index()` helper behind both `/ask` and `/system-docs`. 263 tests green,
`ui/dist` rebuilt, **uncommitted** like session 13. Details:
`context/sessions/2026-07-22-2.md`.

## Session 13 (2026-07-22): fresh codebase review → full remediation

Fresh four-agent review of the whole codebase → findings in
`context/reviews/2026-07-19-codebase-review.md`, actionable plan in
`…-fix-plan.md` (7 conflict-free work units A–G, execution log at its foot).
**All units A–G applied this session; 192 → 261 tests green; NO commits.**

- **A — trust boundary (H1+H2)**: `sanitize_untrusted_metadata` + `trusted=`
  flag so external metadata can NARROW but never WIDEN tools/budget/resume;
  internal spawns marked trusted. Areas strip Bash/unscoped Edit-Write from
  SKILL.md frontmatter at load unless in `security.privileged_areas`.
- **B — graph cap (H3)**: `graph.candidate_ids` threaded into apply; LLM
  `invalidated_ids` intersected with the exact prompt candidate set, bools
  rejected, per-batch add cap.
- **C — chunker (M5+L6)**: junk-word filter tokenizes per-line (keeps prose),
  absent index_dir no longer purges its index.
- **D — voice (H4–H7, M7–M9, L7–L11)**: sub-frame TTS interrupt, barge races
  the queue, mic-loss watchdog, id-clear on ack, non-2xx→spoken error,
  separate barge VAD, control-char injection strip, PTT cap. (3 sub-agents.)
- **E — dispatcher (M1 /stt cap+origin guard, M2 real quick-cancel via
  procs registration + cancelled-guard, M3 busy_timeout=15s, L1 atomic
  add_fact, L2 stderr reaping).**
- **F — memory (M4 block-file allowlist + scoped summarize Write, M6 surface
  failed consolidation + roll episodes back on failure, L3 orphan-vector
  sweep + existence-checked vec insert, L4 DST-correct zoneinfo, L5
  memory-statement divert veto).**
- **G — UI/ops (H8 Piper both-file+atomic guard, M10 postTask resp.ok,
  M11 dev proxy /automations+/stats, M12 AskScreen stable-id patch, L12
  AgentMonitor live-step append+refetch, L13 config fixture test, L14
  skipUnless optional-dep, L15 doctor timer glob, L16 widget mutation
  notes, L17 setup_ask_screen printf %q).** ui/dist rebuilt.

Not built (listed in the fix-plan's "Coverage gaps" section, optional):
POST /task HTTP-layer test, notify-gate wiring test, reflection auto-queue
test, /events SSE contract test, /memory/search routing test.

**Next: user reviews the diff; then commit (per-unit or squashed) on the
user's word.** Do NOT commit unprompted. Two things to remember at commit
time (detail in `context/sessions/2026-07-22-1.md`):
- **ui/dist is gitignored + force-added** — the rebuilt bundle's new hashed
  assets are untracked/ignored; run `git add -f ui/dist/assets/index-lkUaqwu2.js
  ui/dist/assets/index-A83EmG_w.css` or the committed dist references missing
  files.
- **Fold the changed behaviors into CLAUDE.md operational notes** (origin guard
  + /stt cap, memory block-file allowlist, failed-consolidation surfacing +
  rollback, automations DST + statement veto) — deferred to commit time so
  uncommitted behavior isn't documented as live.

## Session 12 (cont. 3): deps landed → Phase I completed + Phase J shipped

- **PDF/image ingest** (finishes Phase I): chunkers take (label, path);
  pdf/epub per-page text w/ OCR fallback (10-page budget); images via lazy
  rapidocr (~1.5s each); DepMissing degrades to dep_gated counting. Live:
  generated PDF + rendered-text PNG both indexed and searchable.
- **Phase J1 hybrid memory**: dispatcher/embeddings.py (fastembed bge-small
  int8, lazy singleton, passage/query split); vec0 tables rowid==id;
  RRF fusion in db.search_*_hybrid; /memory/search hybrid unless filtered;
  backfill at startup/reindex + 15-min refresh loop. Live: 49 entries + 10
  episodes embedded; paraphrase query with ZERO FTS hits surfaced the right
  block via vectors ("preferred coding tool" → USER.md Neovim).
- **Phase J2/J3 knowledge graph**: kg_entities/kg_facts/kg_fact_entities
  (+FTS), bi-temporal invalidate-never-delete; graph-extract quick task
  rides the consolidation export (spawned by /memory/consolidate);
  scope=graph search with 1-hop neighbor join; POST /memory/reconcile +
  weekly timer Sun 04:30 (installed + enabled). Deterministic applies in
  dispatcher/graph.py, unit-tested without LLM.
- **191 tests green** (+17 semantic/graph, +3 PDF/OCR, tests use synthetic
  384-dim vectors — no model download in the suite).

## Session 12 (cont. 2): ask-about-my-screen shipped (context/ssplan.md)

`<Super><Alt>a` → portal area screenshot → chromium --app popup at
`/ask?shot=<id>` → typed/mic question → streamed answer; follow-ups resume
the CLI session (source `screen:<shot>`). New: dispatcher/stt.py + POST /stt
(raw-body Whisper), GET /screenshots (traversal-guarded), GET /ask,
screenshot-aware stream_quick (Read tool + wrap-on-fresh-session),
ui/src/AskScreen.jsx, jarvis/ask_screen.py (jeepney portal),
scripts/{setup,smoke}_ask_screen.sh. 174 tests green (+10); smoke 5/5 incl.
real-audio /stt and a real image-reading quick task. **User: run
scripts/setup_ask_screen.sh** (installs jeepney — pip is blocked for the
assistant) then manual E2E per ssplan step 8.

## Session 12 (cont. 1, 2026-07-19): Phase I — personal OS, dep-free scope, live-verified

- **Ingest expansion** (`dispatcher/ingest.py`): per-format processors —
  `.txt/.text` paragraph chunks, `.html/.htm` via stdlib HTMLParser →
  markdown-ish (headings → ancestry, links keep URLs, script/style dropped)
  → existing chunker; walk generalized from `*.md` to a CHUNKERS map;
  PDF/images counted as `dep_gated` in stats (the visible cost of unapproved
  deps). `vault/inbox/` convention dir + README. Live: .txt indexed +
  searchable + deletion-swept.
- **Standing automations** (`dispatcher/automations.py`, khoj pattern
  re-implemented): one LLM parse (prompt with local now/tz/weekday; vague
  times pinned: morning=07:30 etc.) → `parse_response` JSON extraction →
  `validate_spec` mechanical sanitizing → `automations` table (daily/weekly/
  interval/once; local at_time, UTC next_run_at). **In-dispatcher asyncio
  scheduler loop** (30s) instead of the planned systemd timer — deviation
  justified: dispatcher must be up anyway, catch-up free (past-due fires on
  first check), no boot race. Advance-before-submit so a failing submit
  can't tight-loop. Endpoints: POST/GET /automations, /toggle (re-enable
  recomputes next_run_at), DELETE. **NL divert** in POST /task (mode=auto +
  nl_detect): "every morning at 8, …" → automation + spoken confirmation
  SSE; question-openers veto the divert. Voice source never touches it
  otherwise.
- **Notify-or-not gate** (`dispatcher/notify.py`, khoj notify-check
  pattern): settled automation/timer 'done' results → `notify-gate` quick
  task resuming the run's own session → `NOTIFY: <one spoken sentence>` →
  SSE `notify` event (speech) + notify-send; `SKIP:` → log +
  `notify_skipped` event. Fail-open. `surfacing()` policy is a pure function
  (unit-tested): meta task types (reflection, memory-consolidate,
  notify-gate, automation-parse) are **never announced** — fixes
  pre-existing noise where jarvis spoke "Done: …" for every agentic task
  incl. reflections. done/failed events carry `surface: false` when
  suppressed; `brain_dispatcher.notices()` honors surface + speaks `notify`
  events. Quick-session continuity now skips internal sources and honors
  metadata `resume_session_id`.
- **Dashboard**: Automations widget (list/pause/resume/delete, next-run);
  AgentMonitor ignores the new event names; EVENT_NAMES += notify,
  notify_skipped, automation, automation_created. ui/dist rebuilt.
- **Config**: `dispatcher.automations` block (enabled, nl_detect,
  check_interval_s, notify_gate, notify_sources, notify_desktop). Empty
  block = feature fully off (keeps tests hermetic).
- **163 tests green (+36 in tests/test_personal_os.py).** Live-verified
  end-to-end: NL parse built a correct once-row (tz-aware) for +3min →
  scheduler fired it on the next tick → quick task answered → gate resumed
  its session ($0.0093) → "NOTIFY: Saturn's rings…" delivered (SSE +
  notify-send). POST /task divert spoke "Scheduled: … daily at 08:00."
  Parse cost $0.064. Test rows deleted after verify.
- **First scheduled 02:30 consolidation ran clean tonight** (Phase F timer's
  first unattended fire).
- README.md mystery solved: the user pasted a `claude --resume <session-id>`
  scratch note at the top — left uncommitted, it's theirs.

## Session 11 (cont. 3): Phase H — observability, live-verified

- **Runner switched to stream-json** (`--output-format stream-json
  --verbose`): each message → timeline step (init/text/tool_use/tool_result/
  result, 200-char summaries, elapsed_ms) → `run_steps` table +
  live SSE `step` events (delivers session-3's deferred "stream agentic runs
  to dashboard" item). Result parsing unchanged in shape; `_FakeProc` test
  double rewritten for the stream interface.
- **Quick-path latency**: `dispatcher/telemetry.py` (openjarvis itl.py
  metric shapes, Apache-2.0) — ttft_ms/itl_p95_ms/tokens_per_s per run via
  the new additive-MIGRATIONS loop in db.py; surfaced in done SSE events.
  First live number: CLI backend TTFT 2635ms, 16.75 tok/s.
- **`GET /stats?days=N`** (db.stats_summary) + **`GET /task/{id}/steps`**.
- **Dashboard**: new Observability widget (tiles: tasks/success/cost/TTFT +
  "Jarvis learned" reflection strip); AgentMonitor shows live current-step
  line under running rows, step timeline + ttft/tok-s in expanded detail;
  SSE now subscribes `step` + `requeued`. ui/dist rebuilt and live-served
  (bundle content verified; visual browser pass left to the user).
- **127 tests green (+14 in test_observability.py, _FakeProc rework).**
  Live: quick task carried ttft in its done event; agentic verify task
  produced a 5-step timeline (init→Bash tool_use→tool_result→text→result)
  with 5 SSE step events observed; /stats returned real aggregates
  ($0.99 today across 6 sources, success 100%).
- **Safety observation for later** (noted in CLAUDE.md): headless CLI ran a
  read-only Bash despite allowedTools [Read, Glob, Grep] — pre-existing
  behavior, flag for the Layer-13 pass.

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
