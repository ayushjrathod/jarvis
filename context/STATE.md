# STATE — read me first each session

_Last updated: 2026-08-10 (session 29, end)_

## Session 29 (2026-08-10): system volume phrasings widened — READ THIS FIRST

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

## Session 25 (2026-08-01): maintenance sweep — the brief's false failure

User asked for cleanup + maintenance. Health sweep clean (**439 tests**,
smoke_phase_a **7/7**, doctor all ✓ bar two known `!`, all units active, backup
prune working at 14/14 days, no orphan task rows). It surfaced one real bug and
one long-standing waste. Full detail: `context/sessions/2026-08-01-1.md`.

- **The 07-30 daily brief settled `failed` for $0.51 and shouldn't have.**
  Three things lined up: (1) the agent's `mcp__gmail` grant **never matched
  anything** — the server is `mcp__claude_ai_Gmail__*` — so every brief since
  Phase C requested Gmail and ate a denial; (2) the box was off at 07:30 on
  07-29, so the `Persistent=true` catch-up fired at **00:42 on the 30th** and
  wrote *2026-07-30*.md (hence **no 07-29 brief**); (3) the real 07-30 run
  therefore correctly made no edit — and session 17's mtime guard can't tell
  "denial blocked the write" from "no edit needed", so a tolerable denial became
  a hard failure. Also hit **07-23**. No notify-gate ran either day, so the
  silence looked normal.
- **Fix**: `runner.denial_could_block_output()` only lets
  `Write/Edit/MultiEdit/NotebookEdit/Bash` explain a missing output file — an
  MCP/read tool can't write it. **Unparseable denials stay fatal**: the
  dangerous direction is session 17's silent false success. +7 tests pinning
  both directions, incl. the CLI's real `Write`-denial wording.
- **Gmail MCP has been connected all along** (`claude mcp list` in
  `~/.claude-per`: Gmail ✔, plus Notion/Drive/Calendar/Spotify). The "left to
  user OAuth" item open since Phase C **was already done**; only the wrong grant
  name stood between the brief and the inbox. **User's call: keep the brief
  email-free** — dead grant and the whole Email section removed, and the prompt
  now says not to call the Gmail tools it can still see.
- Live-verified: fresh brief run → `done`, 8 turns, $0.2803, **zero denials**,
  file rewritten. First brief in the project's history with no denial.
- `vault/briefs/test.md` + `refusal-test.md` are **smoke_phase_a artifacts**,
  not debris — tracked on purpose, left alone.
- **Committed** the two-session backlog in batches on the user's word (sessions
  23 + 24 + this fix + the vault briefs).

**Second pass, repo-wide** (user asked for another run): SQLite
`integrity_check` + all three FTS indexes ok, **0 orphan vectors** (82/82
entries embedded), no indexed-but-missing files, no tracked secrets, 31 routes
all documented, zero TODO/FIXME, openwakeword still pinned 0.4.0, **no systemd
unit drift** (all 16 byte-identical to installed), `npm run build` reproduces
the committed dist hashes exactly, `smoke_phase_b` **12/12**. Three fixes:

- **`mission-tailscale-serve` had NOT failed at boot** despite a scary journal
  line — `--list-boots` shows that entry is the *previous* boot's teardown. The
  session-21 readiness wait worked (waited ~9s); tailnet `/health` is 200.
- **Dead config key removed**: `budgets.quick_max_tokens` was the Messages API's
  `max_tokens`, unread since that backend was removed — it would have silently
  ignored anyone trying to cap quick answers (`budgets` loads as a plain dict,
  which is why it survived).
- **Two stale "Messages API" references in code** (session 23 found two in
  docs): `runner.py` told readers to "use the messages_api backend", which
  doesn't exist, and `smoke_phase_b.py` printed "Messages API path will be
  faster" on every run. Both now state the subscription CLI's ~3s TTFT floor.

Noted only: `references/` is **1.3G** of gitignored clones (**user says keep
them**); barge-in measures **193ms against a 200ms budget**.

**Third pass — the memory system was feeding on its own housekeeping.** Backup
verified restorable (41/41 tables), meta routing still on haiku, security clean
(`privileged_areas: []`, no Bash in any area), no unbounded tables, /stats
success 96.4%. Then the real find:

- **Every fact in the knowledge graph was a self-observation** ("the daily-brief
  automation continued writing successfully through 07-31"), 4 of 6 already
  invalidated by the next night's restatement, **zero about the user**.
  `should_capture` excluded the consolidator/reflection/gate tasks — its comment
  says why: *"the consolidator's own run would become next night's input"* — but
  **not `daily-brief`/`weekly-review`**, whose episode is "I wrote a file", so
  `graph-extract` read them back as knowledge nightly. Both now excluded (+2
  tests, incl. one pinning that ordinary `source=timer` tasks are still
  captured). Nothing lost: brief *content* reaches memory via the vault index.
- **Purged (user approved), DB backed up first**: 6 facts + 18 fact-entity links
  + 4 orphan entities, every row asserted against a self-observation guard; and
  the 6 episodes queued for 02:30 (2 brief runs + **4 artifacts of my own
  acceptance tests**) marked consolidated so they can't reach MEMORY.md.
- **The brief now skips the model on a quiet day (user approved)** —
  `dispatcher/brief.py`, deterministic like `spotify.detect`/`desktop.detect`:
  no open tasks + no note touched in 3 days → `run_agent.py` writes the file
  itself, no task submitted. It had written "clean slate" daily 07-24..08-01 for
  ~$0.40 and 8-11 turns each time (vault holds one *done* task from 07-06, two
  notes from 07-05). Fails toward doing the work — an unreadable task file
  counts as material. `brief.skip_model_when_quiet: false` reverts.
  Live: wrote 2026-08-02.md with **0 tasks created**.
- Noted, not acted on: the **learning loop is dormant** — 2 reflections ever
  (last 07-19). Trigger is `num_turns >= 12`; the brief runs 8-11, so normal
  operation never reaches it.

**450 tests green.**

## Session 24 (2026-07-28): computer-use research — what else we can add

User asked for internet research + a `references/` pass on further computer-use
capability. No code changed. Findings in
`context/research/2026-07-28-computer-use-expansion.md`.

- **The session-20 spike was too pessimistic.** It tested two D-Bus surfaces
  (`Shell.Eval`, `Shell.Introspect`) and declared window management unreachable
  without a shell extension. **AT-SPI2 was never tried, and it works here** —
  measured: 9 apps on the a11y bus, window titles readable, and app windows
  expose `Component` (geometry + `grabFocus`) **and `Action` (`doAction` =
  click-a-widget-by-name, no coordinates)**. That is the deferred capability
  plus the semantic clicking the Hermes audit called the sweet spot.
- **The seam question is settled by session 22.** Anthropic's native computer
  tool is a Messages API tool; the subscription CLI can't reach it. So the only
  channel is a **local MCP server** granted via `--allowedTools mcp__…` — which
  is exactly what the existing least-privilege + trust-boundary machinery wants.
- **`cua-driver`: recommend dropping.** Its Linux backend is X11/XTEST; native
  Wayland is preview-only behind an opt-in flag, with native-Wayland apps
  potentially invisible. This box is GNOME 50.2/Wayland.
- **Reachable, unnoticed until now**: portal `RemoteDesktop` **v2** (persist +
  restore token — the sanctioned Wayland input channel, jeepney-reachable),
  `libei` 1.6.0 installed, `GlobalShortcuts` portal v1, and node 22 with a
  built-in `WebSocket` (CDP with zero deps, already proven twice in-repo).
- **Ranked**: browser control (T3, zero deps — or one npm dep for Google's
  official `chrome-devtools-mcp`, ~29 tools) > AT-SPI window/semantic control
  (T2) > a self-service screenshot (precondition for any pixel work) >
  RemoteDesktop input (correctness upgrade; ydotool already works).
- Four open questions for the user at the foot of the research doc — including
  the one dep decision AT-SPI needs (`pygobject` in the venv vs raw jeepney).

## Session 23 (2026-07-27): "open firefox" actually opens firefox

User: "when i say open firefox or any other installed app it should open it."
It had **never** worked, while answering "Opening firefox." every time — three
bugs in series, each hidden by the next. **432 tests** (415 → 432). Full detail:
`context/sessions/2026-07-27-3.md`. **Uncommitted.**

1. **No display in the service environment.** A systemd *user* service only
   carries `DISPLAY`/`WAYLAND_DISPLAY` if it started after GNOME ran
   `systemctl --user import-environment`; at boot it doesn't, and the
   dispatcher's env had neither. firefox died with "no DISPLAY environment
   variable specified" while **gtk-launch exited 0**. `desktop.session_env()`
   now borrows the missing vars from the systemd user manager (survives a
   reboot; a unit-file EnvironmentFile would not). The clipboard verbs had
   worked only by luck — `wl-paste` falls back to the `wayland-0` socket.
2. **A successful launch then hung.** `_run`'s `capture_output` waits for EOF
   and the app holds those pipes for its whole life, so a *working* launch
   blocked 10s ("gtk-launch didn't respond") and a *failed* one returned at
   once. GUI spawns now go through `desktop.spawn_app()` (DEVNULL).
3. **The app would die with the dispatcher.** Children inherit our cgroup, so a
   routine `systemctl --user restart mission-dispatcher` would kill everything
   it had opened. `spawn_app` wraps the launcher in `systemd-run --user --scope
   --collect --slice=app.slice` (GNOME's own `app-*.scope` mechanism).
   Verified: firefox survived a restart.

**No more false success**: `_launch` checks the process itself (`Exec=`
basename, or the desktop id for `flatpak`-style wrappers) and says "<app>
didn't start" otherwise. Matching is on **argv[0]** — the first live test of the
check passed for the wrong reason, having matched my own shell command line that
merely contained the word "firefox". `spotify.ensure_running` shared bugs 1 and
3 and now shares `spawn_app`. `doctor.sh` gained the display check that would
have caught all of this.

Live: `POST /desktop` and the `POST /task` mode=auto divert both open real apps
(firefox, calculator, text editor); an unknown app still fails honestly.

**Docs pass** (same session): README (desktop bullet, new troubleshooting row,
test count), the in-app `/system-docs` page (`ui/src/docs/content.js`, rebuilt →
`index-CrUrEtAU.js`, CSS unchanged, served live), `future/computer-use.md`
(what T1 got wrong + the lesson for T2/T3), and `future/phone-access-and-feature-
gaps.md`. Two **session-22 leftovers** caught doing it: README still credited the
removed API backend with halving voice latency, and the feature-gap doc still
called that backend "production-ready" as a second provider path.
**Reminder for commit time**: `ui/dist` is gitignored + force-added, so
`git add -f` the two new hashed assets.

## Session 22 (2026-07-27): API key dropped — subscription only

User: "i will be not adding credits to api key so remove its usage rely on
claude code." Done — commit `9b949ec`. **415 tests** (439 → 415; the API path
took 29 tests with it, 4 focused `cli_env` tests replace them).

- **`dispatcher/quick.py` 400 → 163 lines**, one backend. Removed
  `_stream_api`, `HistoryStore`, `ApiAbort`, the pooled client, the
  unusable-key fallback/cooldown, `resolve_backend`, the `quick_backend` /
  `quick_history_turns` config keys, `scripts/smoke_messages_api.py` and
  `tests/test_quick_api.py`. All recoverable at `ce93ea0`.
- **Supersedes half of locked decision #2** ("quick Q&A → streaming Messages
  API"). Substance unchanged (quick streams, agentic runs headless), mechanism
  changed. Written into CLAUDE.md explicitly, since that decision is otherwise
  marked do-not-re-evaluate.
- **`runner.cli_env` kept and now more important** (own test file): the CLI
  *prefers* an API key over the claude.ai login, so a stray key **replaces**
  the subscription. The unit lost the `EnvironmentFile` it gained yesterday;
  `doctor.sh` now flags an exported key as a hazard, not an opportunity.
  `.env` is untouched on disk (yours, gitignored) but nothing reads it.
- **Real pre-existing bug found by running the acceptance suite**:
  `task_types.summarize` granted `Write(vault/**)`, and a bare `Write(path)`
  rule is **silently ignored** by this CLI — the write is denied. Probed
  directly: `Write(vault/**)` alone → "I need your permission";
  adding `Edit(vault/**)` → file written, zero denials. Session 13 (M4)
  introduced the scoping, session 17's denial detection surfaced it, and the
  daily-brief agent already had the right form plus a comment. **smoke_phase_a
  back to 7/7.**
- Live after restart: key absent from the dispatcher env, quick answer used
  the memory blocks ("Neovim"), follow-up resumed the session ("spell that
  backwards" → "mivoeN").

**Next:** computer-use **T2/T3** is the only substantial unbuilt item (see
`future/computer-use.md`); T3 needs no new dependencies.

## Session 21 (2026-07-27): Messages API backend made production-ready

> **SUPERSEDED by session 22 (above): this backend was removed the same day.**
> Kept because the *measurements* below are still the reference for what the
> subscription CLI path costs, and because the two traps it documents (a key
> replacing the subscription; `cli_env`) still apply. **Ignore the
> "flip to `auto`" instruction** — there is no `quick_backend` setting any more.

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
3. ~~Gmail MCP~~ — **done, and then deliberately unused.** The server is
   connected in `~/.claude-per`; the user chose (2026-08-01) to keep the daily
   brief email-free, so the grant and the Email section were removed. Re-enable
   by granting `mcp__claude_ai_Gmail__search_threads` + `…__get_thread` in
   `areas/tasks/agents/daily-brief.md` and restoring the section — no code
   change needed.
4. ~~`ANTHROPIC_API_KEY`~~ — **obsolete.** The Messages API backend was removed
   2026-07-27; a stray key now actively *replaces* the subscription and is
   stripped from every `claude` subprocess (`runner.cli_env`).

## Known caveats (details in CLAUDE.md + session logs)

- Barge-in needs headphones or PipeWire echo-cancel.
- `Write(path)` allowedTools rules silently ignored — use `Edit(path/**)`.
- Area triggers match as in-order word subsequences (dispatcher/areas.py).
- Timers no-op (journal an error) if the dispatcher is down.

## Deferred (per spec, do not build unprompted)

Other life areas, cloud Routines, multi-agent fan-out, media area (mpv/yt-dlp),
Kokoro TTS swap.
