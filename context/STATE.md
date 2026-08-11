# STATE — read me first each session

_Last updated: 2026-08-11 (session 29, end)_

## Session 29, part 3 (2026-08-11): standing list swept — READ THIS FIRST

Session 29's volume work is **committed** on `feat/system-volume-phrasings`
(branched off main at `a5f1fe8`). **736 tests.** Then, on the user's pick,
three standing items:

- **Both voice units could flap forever without ever failing.**
  `mission-jarvis` and `mission-dictate` ran `Restart=on-failure` +
  `RestartSec=5` with no StartLimit — at that pace only ~2 starts land in
  systemd's default 10s window, so the limit never trips and a crash-on-startup
  loops as "activating" indefinitely. Session 28 fixed exactly this on the
  dispatcher and missed the two units where it matters more: nothing else tells
  you Jarvis has gone deaf. Both now match the dispatcher (300s/5/10s),
  installed + `daemon-reload`ed **without restarting either service** (systemd
  picked the values up; both still active). A test now pins
  `burst * RestartSec < interval` for every restarting unit.
- **`graph_extracted_at`** — the graph now tracks its own hand-off, marked
  **only on success**, so a rolled-back consolidation no longer re-feeds
  episodes the extractor already digested. It also gets its own rendered subset
  (`memory.render_episodes`) instead of the whole consolidation export.
  `add_fact`'s duplicate guard goes back to being a backstop rather than the
  thing holding the line. Additive migration; applied live, integrity ok, 82
  episodes intact.
- **`ConfirmBar` expires client-side** — the `confirm` SSE event now carries
  `timeout_s` and the banner counts down and removes itself. It used to keep
  offering **Yes** on an id the dispatcher had already dropped. Verified in a
  browser (chromium + CDP) against an **isolated** dispatcher on :8799 with
  `confirm_timeout_s: 8` — banner appeared, ticked 7→1, vanished at 8s; the
  clipboard was never read. Real dispatcher and DB untouched.
- **`Config.brief`** landed too (`scripts/run_agent.py` no longer re-reads
  config.yaml by hand).

**Still open:** the `Host` header check (needs one live check of what Host
Tailscale Serve forwards from your phone) — that is now the whole code list.
User-side: rotate the `sk-ant-` key, `git rm` the two smoke artifacts
(`vault/briefs/test.md`, `refusal-test.md`) + reindex, reboot test, phone
add-to-home-screen, ask-screen/Spotify E2E, and **merging this branch**.
Biggest unbuilt feature remains K2 (computer-use T2 via AT-SPI, T3 browser over
CDP with zero new deps).

Two gotchas worth keeping: `pkill -f "remote-debugging-port=9222"` matches the
issuing shell's own command line and kills it (exit 144); and an isolated
dispatcher's config must live **in the repo root**, because `Config.root` is
the config file's parent — put it in /tmp and `ui/dist` isn't found, so `/`
serves the API JSON instead of the SPA and every DOM check silently fails.

## Session 29 (2026-08-10): system volume phrasings widened

User asked to "add system volume control". **It already existed** — computer-use
T1 has had `volume`/`mute`/`status` over wpctl since session 20, live and
policy `allow`. What was missing was *reach*: the parser knew four shapes, and
everything else a person actually says — "turn **up** the system volume",
"raise/lower the system volume", "volume up on my computer", "mute my computer"
— fell through to a real `claude -p` call, ~$0.10-0.14 and ~3s to do what wpctl
does instantly for nothing. I said so, offered four options, and the user chose
**widen the phrasings**. Full detail: `context/sessions/2026-08-10-2.md`.
**Tests 721 → 728, green.**

- Parser-only change in `dispatcher/desktop.py` — no executor, service or
  config change, `Intent` shapes unchanged, so the policy plane / confirm flow
  / divert / SSE are all untouched.
- Relative phrasings are now five patterns split by sentence shape
  (`_VOL_REL_RES`): a *leading* direction word is a verb (raise/increase/boost/
  lower/decrease/reduce/drop), a *trailing* one is an adverb (up/down/louder/
  quieter/softer). Absolutes gained `NAMED_LEVELS` (max/full/half/quarter/zero,
  "all the way up") and a trailing-qualifier form. Mute gained "silence …" and
  "turn the sound off/on"; status gained "is the system muted", "how loud is
  the computer", "what volume is the system at".
- **The qualifier invariant held**: bare "max volume" / "half volume" / "make
  it louder" are still NOT desktop commands, because bare "volume 40" is
  Spotify's and splitting one sentence shape across two subsystems is exactly
  what that rule prevents. Verified generatively — 8555 sentences the desktop
  parser now claims, **zero** of which `spotify.detect` also claims.
- Live: dispatcher restarted onto the new code (clean, no journal warnings);
  `POST /desktop` ran all four new phrasings; `POST /task` mode=auto diverted
  "raise the system volume" to `kind: desktop` with no task row and no Claude
  call. **System volume captured and restored to its original 16%.**

### Then a subagent review of that change, which found three real defects

User asked for it explicitly. The reviewer verified by execution (grammar
expansion, mutation testing, replay of 244 historical task texts). All fixed,
**728 → 732 tests**:

1. **"machine state" executed as a status command.** Adding `machine|pc|
   speakers?` to `_SYS` made `_STATUS_RE`'s bare "<qualifier> status|state"
   shape claim "machine state" / "pc status" / "what is the machine state" —
   an ordinary question about a state machine or a VM, answered with the volume
   report and filed done without ever reaching a model. The bare shape now uses
   the narrower `_SYS_CORE`; the new words only ever qualify an audio noun.
2. **My negative test pinned nothing, and STATE.md + CLAUDE.md both asserted it
   did.** `test_ordinary_english_is_not_a_volume_command` ("raise an exception",
   "lower my expectations") was meant to pin the qualifier rule — but none of
   its inputs carries an audio noun, so they fail on the noun instead. Proven
   by mutation: making the qualifier optional leaks "raise the volume" and the
   suite **still passed**. Now pinned by
   `test_the_system_qualifier_is_what_keeps_them_apart`, and I re-ran the
   mutation to confirm it fails (it does, along with the older bare-volume
   test). **Lesson worth keeping: a negative test proves nothing unless its
   inputs can only fail for the reason being claimed.**
3. **Coverage gap in the very thing the change was for.** The trailing-
   qualifier form only worked with the noun *before* the direction word, so
   "turn up the volume on my computer", "raise the volume on my pc" and
   "lower the volume on the laptop" still cost a real `claude -p`. Two more
   patterns added, plus `crank/bump/kick/dial up`, "put … up", the possessive
   ("turn up my computer's volume") and a **trailing** "please" strip —
   `_normalize` only ever stripped the leading form.

Also from the review: `_DOWN_WORDS` is a denylist, so a direction word added
and forgotten silently turns the volume **up** on an `allow`-policy verb —
`_UP_WORDS` + a partition test now pin it. Re-verified after all fixes: 0
Spotify collisions, `parse_answer` and the case-preserving args unaffected.

**Offered and NOT built** (your call if you want them): a dashboard volume
widget — there is no UI surface for desktop control at all today, only a typed/
spoken command or `POST /desktop`; and giving the *system* the bare verbs, which
would reverse media-first precedence so Spotify's volume needs "music volume
40". Session 28's standing list is untouched.

## Session 28 (2026-08-10): worked the standing list

User asked to keep going on the remaining review findings, under a hard
constraint: **no API key, no credits**. Everything is offline — source fixes plus
LLM-free unit tests. No smoke script, no `claude -p`. Full detail:
`context/sessions/2026-08-10-1.md`. **Tests 550 → 721, green.**

Five batches (voice, memory, dispatch core, ops, HTTP coverage). The ones that
would have bitten you soonest:

- **The queue watcher could run a task up to 4×** — it submitted *before* renaming
  the file into `.processed`, and a rename failure is on the transient-retry
  whitelist. Now claims the file first.
- **A wall-clock timeout re-ran the whole agentic task** over files the first
  attempt had already edited, and lost that attempt's cost entirely from `/stats`.
- **Every voice barge-in wrote a bogus `failed` run row** ("claude exited -9") over
  the honest `cancelled`.
- **A missing parser dep DELETED index rows** — an onnxruntime upgrade breaking
  rapidocr would have silently removed every OCR'd document from search.
- **One unreadable `SKILL.md` 500'd every `POST /task`** (areas are re-read on
  every dispatch, and reflection/learn runs write those files).
- **Code blocks are no longer read aloud** — a 1029-char block used to produce five
  chunks of raw `rm -rf` shell.
- **Timer units had no `TimeoutStartSec`** while reconcile's own retry budget
  reached ~61 minutes against systemd's 90s default — SIGTERM'd and journaled as
  failed while the dispatcher kept working.
- **`tests/test_http.py` (94 tests)** where the HTTP layer had none — and it caught
  a bug I introduced on 08-08: the divert renderer in `main.py` hardcoded
  `status: "done"` while `_settle_divert` derived it, so an unresolved play was
  reported to the dashboard as a success.

**Note:** a `create_app(Config.load('config.yaml'))` sanity check instantiated a
Service against the **live** `data/mission.db` and wrote to it — returning 4
episodes stranded by a consolidation that failed on 08-07 to the pool. That is the
repair the new code exists to do and nothing else was touched (no task was
queued/running; `/health` fine), but it was unintended; use a temp config for that
check.

Standing list is now short — see the session log's "Still not done": the `Host`
header check (needs a live tailnet check first), `graph_extracted_at`, the
jarvis/dictate restart loop, `Config.brief`, ConfirmBar client-side expiry.

## Session 27 (2026-08-08): second full review + fixes — READ THIS FIRST

Whole-codebase review with six parallel reviewers, then fixes. Full detail:
`context/sessions/2026-08-08-1.md`. **492 → 550 tests, green. Nothing
committed** — sessions 26 and 27 are both sitting in the working tree.

**The important finding: session 26's own divert fix introduced two bugs.**
`_settle_divert` filed *every* divert `done` — including a parked confirmation
nobody can answer, a denied verb, and a failed automation parse (the normal
outcome during a plan-cap window, which left the request neither scheduled nor
run) — and announced **nothing**, because it fires `done` with `kind="quick"`
and the voice client only speaks `kind == "agentic"` or a `notify` event.
`NEVER_GATE_TASK_TYPES` was unreachable dead code with a passing test over it.

Fixed this session, each verified by executing it:

- **degraded mode was dead** — the relevance anchor AND-ed every token including
  stopwords, so no natural-language question ever anchored; now per content
  word, and the "unladen swallow" property still holds
- **the confirm plane was defeated by "okay so …"** — `parse_answer` matched a
  prefix, so any sentence starting with an affirmation approved a parked
  `clipboard_get`/`open`
- **`run the tests` was executed as an app launch** (policy `allow`, no confirm)
- **`media-parse` was feeding the knowledge graph** (live episode 62, already
  consolidated) — same mechanism as 5f200f3
- **the degraded answer was captured as an episode**, so an outage message could
  be quoted back as memory
- **`simulate_refusal` was outside the trust boundary** — any caller could force
  a second run on the Opus fallback; now gated on
  `security.allow_simulate_refusal` (false)
- **the quick path ignored `metadata["allowed_tools"]`**, so `inbox.summarize`
  could not read the file it was told to read
- **`automations.detect` diverted questions** (no wake-word strip, no `?` veto)
- **`inbox.summarize: true` was worse than useless** — replaced the free notice,
  delivered its summary nowhere, and fanned out uncapped on a synced directory

Plus VETO word boundaries in both parsers, `policy:` fail-closed, `open the
door` (pre-existing — checked against HEAD before blaming the new code), and
smoke-script cleanup.

**Then the UI batch, all five items** (`ui/dist` rebuilt): `sw.js` no longer
caches every navigation as the app shell (one click on "docs →" used to poison
the installed PWA permanently); SSE now reconnects with backoff and says so —
EventSource gives up entirely on a non-2xx reconnect, which is what a dispatcher
restart looks like through Tailscale Serve; widgets re-seed after an outage;
the mic no longer leaks a live recorder on a double-tap; degraded answers are
labelled instead of replaced by the raw CLI error; and a new `Notices.jsx`
renders `notify` events, which reached the browser and were displayed **nowhere**
— the whole proactive-notification feature was invisible on the phone.

Verified headlessly (chromium + CDP) against an **isolated** second dispatcher
on :8799 — the real one on :8765 was never touched. A queue file → divert →
`notify` → *"Right now: system volume 16%, screen unlocked."* rendered on the
390px dashboard, which exercises both this session's divert fix and the new
component. `kill -9` → banner; restart → recovers.

**Still open in the web surface:** no `Host` header check, so DNS rebinding can
*read* the vault/tasks/screenshots (writes stay blocked). Left deliberately —
the allowlist has to match whatever `Host` Tailscale Serve forwards, and a wrong
guess locks the phone out. Needs one live check first. The full standing list
(voice, memory, core, ops, coverage) is at the end of the session log.

**Awaiting your call:** `vault/briefs/test.md` and `refusal-test.md` are smoke
artifacts, tracked in git and indexed for `/memory/search`. The script now
cleans up after itself; those two need `git rm` + a reindex. I didn't delete
vault content unilaterally.

## Session 26 (2026-08-02): full-codebase review + the fixes it found

User asked for a thorough senior-engineer review of the whole codebase, then to
fix what it turned up. Read every dispatcher module, the voice pipeline, the UI,
config, units and scripts. Full detail: `context/sessions/2026-08-02-1.md`.
**Nothing committed — awaiting user review.**

The architecture holds up; **almost every bug was in a seam between two
subsystems that were each individually correct.** Eight fixed:

- **The diverts only existed in the HTTP handler.** `automations.fire` and the
  queue watcher call `svc.submit()` directly, so "every morning play jazz"
  correctly became a standing automation — and then handed **"play jazz" to
  Claude as an agentic task every morning** instead of to Spotify. The chain now
  lives on `Service.try_divert()`; `main.py` renders it (SSE byte-identical,
  still no task row), `submit()` uses it for `mode == "auto"` and records the
  result as a task **born settled** (no run row). Diverts skip the notify gate
  (the executor already wrote the summary) and episode capture (else the graph
  eats an identical "play jazz" every morning — session 25's lesson). An
  automation can never create an automation.
- **Deleting a file from `vault/inbox/` announced "Indexed 0 files"** — the
  notify branch fired on `removed` but formatted `arrived`.
- **`/stats` `by_source.n` counted runs, not tasks** — `COUNT(*)` over a LEFT
  JOIN to runs. Real db, same window: **old 49, new 46**.
- **Desktop intents lowercased their arguments** — `copy Hello World …` stored
  `hello world`, and a YouTube URL became a **different video**.
- **H2 was bypassed on the consolidation path**: `build_consolidation` parsed
  agent frontmatter itself instead of via `AreaRegistry`, so a reflection run
  (which holds `Edit(areas/**)`) writing `- Bash` into `consolidate.md` handed
  the nightly run a shell — simulated and confirmed. Now one source of truth.
- **`Origin: null` passed the CSRF guard** (`""` was in `_LOCAL_HOSTS`).
- **A dispatcher crash in `stream_quick` was filed `cancelled`**, i.e. read as a
  user barge-in and kept out of the success rate. Now `failed`.
- **`ingest_vault` held vanished files in the index** until a restart.

Deliberately **not** changed, with reasons in the session log: `task_type` /
`agent` tool selection (I flagged it, then talked myself back out — both select
among *operator-authored on-disk* manifests, and locking them re-breaks the
timers), `simulate_refusal` (smoke_phase_a needs it), unbounded quick-path
concurrency, stranded voice notices, the unsurfaced `degraded` flag, ConfirmBar
client-side expiry.

**Then a cleanup pass** (user asked "what else?", approved the top four):

- **A live `sk-ant-` key was sitting in `.env` at the repo root** — never
  committed, gitignored, stripped by `cli_env`, and **doctor.sh had been warning
  about it**. Nothing reads it (the API path went 2026-07-27). *Moved*, not
  deleted, to `~/.local/share/mission-control/env.removed-from-repo-2026-08-02`
  (600). **Rotate that key** — it lived in a working tree.
- **`ui/dist/` was gitignored AND tracked**, so a rebuilt dashboard would
  silently not commit: git keeps tracking existing files, but Vite emits
  content-*hashed* names, so a new bundle is a new path the rule hides — you'd
  commit an `index.html` pointing at a missing asset. Proven with a fake
  `index-<hash>.js`; ignore rule removed.
- **Dead since the Messages API removal**: `Config.quick_cost()`, the `prices:`
  block that fed it, and the never-read `models.classifier` key. All confirmed
  zero-reader by grep first.
- **Tests for the three modules that had none.** `quick.py` mattered most —
  every test patched `quick.stream`, so `_stream_cli`'s stream-json parsing (the
  highest-churn contract here) was entirely unverified while its agentic sibling
  has had fake-process tests since Phase H. +`test_quick.py` (15),
  +`test_bus.py` (9, EventBus drop-on-full + HookRegistry isolation).

**492 tests green** (450 → 468 fixes → 492 cleanup). Dispatcher restarted onto
the new code and trimmed config: `/health` ok, no journal warnings, live free
desktop divert answers, `/stats` totals agree, `doctor.sh` all ✓ bar the
expected "spotify launched on demand". `smoke_phase_a.sh` **not** run — it
spends real quota and the plan cap has taken the assistant down twice.

Deferred, offered as their own change: frontmatter parsing duplicated 5×,
`_normalize`/`VETO`/`parse_response` duplication, **STATE.md is 66KB** and the
protocol reads it every session, `create_app` is 452 lines, 2 undocumented
routes, `jarvis/ptt_dictate.py` is referenced by nothing, `_conn()` reconnects
per query.

## Current phase

**Everything planned is built.** Phases A–J are implemented and live under
systemd; so are the unplanned additions (ask-screen, media control, desktop
control T1, phone access + PWA, degraded mode, inbox watcher). Three
whole-codebase reviews have been run and remediated (sessions 13, 26, 27) plus
the standing-list sweeps in 28 and 29. **738 tests green**, `main` pushed to
origin at 2026-08-11.

The only unbuilt item of substance is **K2** — computer-use T2 (window control
via AT-SPI, which session 24 measured working after the session-20 spike
wrongly declared it unreachable) and T3 (browser control over CDP, zero new
deps). Offered and deferred three times.

Deliberately unbuilt, do not build unprompted: H4 complexity tiers, media area,
Kokoro TTS swap, multi-agent fan-out, the approval queue (nothing outbound
exists to gate yet).


## What runs where (verified live 2026-08-11)

- **4 services enabled + active**: `mission-dispatcher` (dashboard + API on
  http://127.0.0.1:8765, crash auto-restart verified), `mission-jarvis`
  (`--mode both`: "hey jarvis" + hold Right Ctrl), `mission-dictate`
  (hold F9 → typed at cursor), `mission-tailscale-serve` (oneshot, tailnet
  HTTPS at https://archlinux.tail79c6ce.ts.net).
- **6 timers enabled**: daily-brief 07:30 · weekly-review Sun 18:00 ·
  backup 03:30 · memory-consolidate 02:30 · memory-reconcile Sun 04:30 ·
  skill-curator 1st of month 04:00.
- All three long-running services now fail *visibly*: `StartLimitIntervalSec=300`
  / `StartLimitBurst=5` / `RestartSec=10`, so a crash-on-startup reaches
  `failed` instead of flapping as "activating" forever (2026-08-10/11).
- **738 unit tests** green. `smoke_phase_a.sh` 7/7 and `smoke_phase_b.py` 12/12
  as of session 25 — both spend real quota, so they are not run every session.


## What the user still needs to do

1. **Reboot test — never run, and now the biggest unknown.** Phase E
   acceptance: reboot, then `journalctl --user -u 'mission-*' -b` should show
   clean startups. It matters specifically because a user service at boot has
   no `DISPLAY` (why `desktop.session_env()` exists) and `mission-tailscale-serve`
   already died once on that class of boot race. `loginctl enable-linger` is
   already on.
2. **While you're there**, four things batch with that one reboot: open the
   dashboard on your phone (add-to-home-screen) and **tell me the `Host` header
   Tailscale Serve forwards** — that unblocks the last open code item; plus the
   ask-screen E2E (`<Super><Alt>a`) and the Spotify E2E ("hey jarvis, play …").
3. **Rotate the `sk-ant-` key** — it sat in a working tree until 2026-08-02
   (never committed; moved to
   `~/.local/share/mission-control/env.removed-from-repo-2026-08-02`).

Done and struck from this list: the two smoke artifacts
(`vault/briefs/test.md`, `refusal-test.md` — `git rm`'d 2026-08-11 with your
go-ahead, index swept, and `smoke_phase_a.sh` recreates and removes them
itself), the two merged branches, ydotool + `input` group (both active),
Gmail MCP (connected, then deliberately unused — the brief is email-free by
your call 2026-08-01), `ANTHROPIC_API_KEY` (obsolete; a stray key now replaces
the subscription and is stripped from every `claude` subprocess).


## Known caveats (details in CLAUDE.md + session logs)

- Barge-in needs headphones or PipeWire echo-cancel.
- `Write(path)` allowedTools rules silently ignored — use `Edit(path/**)`.
- Area triggers match as in-order word subsequences (dispatcher/areas.py).
- Timers no-op (journal an error) if the dispatcher is down.

## Deferred (per spec, do not build unprompted)

Other life areas, cloud Routines, multi-agent fan-out, media area (mpv/yt-dlp),
Kokoro TTS swap.

## Older sessions

Sessions 3–25 were rolled into `context/state-archive.md` on 2026-08-11 —
verbatim, nothing condensed. Full per-session detail lives in
`context/sessions/YYYY-MM-DD-N.md`. Keep this file to roughly the last five
sessions: the protocol reads it in full at the start of every session.
