# STATE — read me first each session

_Last updated: 2026-09-24 (session 37, end)_

## Session 33 (2026-08-08..25): remediation weeks — READ THIS FIRST after 30

Sessions 31–33 work the session-30 review's suggested order on
`history/aug-sep-backfill` (sessions 26–28 rolled verbatim to
state-archive.md 2026-09-06). **Tests 738 → 811, full suite green.**
Per-session detail: `context/sessions/2026-08-03-1.md`,
`2026-08-07-1.md`, `2026-08-25-1.md`.

- **31 (08-03)**: vault privacy cordon — personal subtrees ignored +
  untracked (READMEs/notes stay), decision recorded, hygiene pins. The
  .gitignore itself wasn't staged (caught 08-08, committed as itself).
- **32 (08-05..07)**: Tier 0/1 — backup retention, checkbox rewrite, shared
  task-status reader, quiet-brief ping, local today, Edit(\*\*) hole,
  trust-boundary 2.4, notify-gate fence, desktop honest status, media
  idioms, spent-once, HTTP seam follow-ups.
- **33 (08-08..25)**: inbox honesty, media edges, event loss (bus eviction +
  flushSync + reconcile poll), BM25 fallback, degraded contractions, runner
  verdict, run settlement, graph clip/retirement/vec accounting, deafness
  trilogy, backup verification, doctor gaps, frontmatter unification, four
  route slices out of create_app.
- Still open: scheduler residue (done 08-27), Tier-3 voice/UI, K2, volume
  widget, Host-header check, publish-history purge-vs-private.

## Session 30 (2026-08-13): full review against the reference repos

User asked for a review of the codebase **in its entirety**, with subagents
comparing against `references/` (all `git pull`ed first — 10 of 12 had upstream
commits), spawned **one at a time** for usage reasons. Eight slices: dispatch
core · memory/graph · personal OS · learning loop + areas · parsers/safety plane
· voice · dashboard UI · ops/config/vault.

**81 findings. Nothing was fixed.** Tree clean, 738 tests green throughout; only
`context/` and the documentation corrections were written.

The full report — tiered, with `file:line`, repros and a suggested order — is at
`context/reviews/2026-08-13-full-codebase-review.md`. It is **uncommitted and
gitignored on purpose**: it details a live escalation path and this repo is
public, so it exists only on this machine (user's call, 2026-08-13). Commit it
once the repo is private or that finding is fixed. Session log:
`context/sessions/2026-08-13-1.md`.

**The five that need a decision or a fix first:**

1. **`vault/` is on a public GitHub repo.** 42 tracked files — `memory/USER.md`
   (rewritten nightly from *every episode*), `MEMORY.md`, tasks, 36 briefs.
   `"private": false`, re-confirmed by hand. `vault/inbox/` is **not** ignored,
   and its README invites dropping exports, so a bank statement dropped there is
   committed by the next `git add -A`. The briefs look deliberately committed,
   so this is **your decision**: gitignore the personal subtrees, or make the
   repo private. (`queue/` is the same class — `.processed/`/`.failed/` are
   ignored *with a comment explaining why*, the live files aren't.)
2. **`Edit(**)` survives the H2 sanitizer** (`areas.py:22-31`) —
   `_is_privileged_tool` reads "contains a parenthesis" as "is scoped", and
   doesn't know `MultiEdit`/`NotebookEdit`. Proved end to end: a reflection run
   whose parent read attacker-controlled text writes an unscoped grant into a
   SKILL.md; the next ordinary task — or the 07:30 brief timer — runs with
   repo-wide write, which reaches `config.yaml` and buys real Bash after that.
3. **Three ways Jarvis goes deaf while systemd reports healthy**, the worst
   being that `--mode both` — the mode the service runs — can never execute its
   own "exit so systemd restarts us" path (the wake thread pins the interpreter
   forever). Only `--mode ptt` exits cleanly, which is presumably where the
   2026-08-10 supervisor fix was tested.
4. **Both nightly memory agents are told today is yesterday** — UTC `{{DATE}}`
   against local `OnCalendar` at UTC+5:30, so 02:30 local fires at 21:00 UTC the
   previous day. Every date in `USER.md`/`MEMORY.md` and every `kg_facts.valid_at`
   lands a day early. `run_agent.py` uses *local* time for the same placeholder,
   so the brief and the memory blocks disagree.
5. **The desktop/media parsers claim ordinary English and file it `done`** —
   `open tasks`, `run migrations`, `put on the kettle`, `play it by ear` — because
   every executor failure is reported as success. `start server` resolves to the
   Avahi SSH browser here.

**Two quiet regressions**: the morning brief notification has been gone since
**08-03** (the quiet path writes the file without submitting a task, so it never
reaches the notify gate), and the dashboard task checkbox has **never** worked
(`vault.toggle_task` writes the file back byte-identical and returns success —
one `status` field, three parsers that disagree).

**Deadline**: `backup_db.sh`'s retention (`-name 'mission-*.db' -mtime +14`)
matches `mission-pre-purge-20260802T000402.db` — the recovery point taken before
the knowledge-graph purge. **Eligible ~2026-08-17.**

Also worth knowing: **a truncated backup passes `integrity_check`** (256KB, zero
tables, reported `ok`) and nothing ever verifies one; and **openjarvis contains
no VAD, wake-word, mic or barge-in code at all** — CLAUDE.md's reference table
was wrong about it. What *is* worth lifting from it is its executor/error/
loop-guard modules.

## Session 29, part 3 (2026-08-11): standing list swept

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

## Current phase

**Everything planned is built.** Phases A–J are implemented and live under
systemd; so are the unplanned additions (ask-screen, media control, desktop
control T1, phone access + PWA, degraded mode, inbox watcher). Three
whole-codebase reviews have been run **and remediated** (sessions 13, 26, 27)
plus the standing-list sweeps in 28 and 29. **876 tests green** on the
`history/aug-sep-backfill` branch (sessions 31–37: review remediation,
Tier-3, K2 T2+T3 dep-free, route split, widgets, routing matrix), `main`
pushed to origin at 2026-08-11.

**A fourth review (session 30, 2026-08-13) has been run and NOT remediated** —
81 findings, none fixed, in `context/reviews/2026-08-13-full-codebase-review.md`
(local-only, see the session 30 block above).
That is now the largest piece of open work, and it outranks K2. It also
disproved several claims CLAUDE.md and the docs stated as fact; those were
corrected in session 30, but treat any remaining "verified" claim in the
operational notes as a hypothesis until re-checked.

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
  **Caveat found in session 30**: `mission-jarvis` can still hang instead of
  exiting — in `--mode both` the supervisor's fatal path never terminates, so
  PTT can be dead while the unit reports `active (running)`. And
  `mission-tailscale-serve` has no `Restart=`, so a dispatcher that reaches
  `failed` deactivates it via `Requires=` and restarting the dispatcher does
  **not** bring it back — phone access dies silently.
- **876 unit tests** green (branch `history/aug-sep-backfill`, sessions 31–37).
  `smoke_phase_a.sh` **8/8** (the script has 8 `ok()`
  calls; "7/7" was stale) and `smoke_phase_b.py` 12/12 as of session 25 — both
  spend real quota, so they are not run every session.
- `doctor.sh` does **not** check `mission-jarvis` or `mission-dictate`, does not
  check whether a timer's service failed on its last run, and does not look at
  the backup at all (session 30).


## What the user still needs to do

0. **Vault on a public repo — half fixed 2026-08-03.** Personal subtrees
   (`briefs/`, `memory/`, `tasks/`, `inbox/*`, live `queue/*.md`) are now
   ignored and untracked (READMEs + dev notes stay); `git add -A` can no longer
   sweep in a bank statement. Still open: the 39 files are **already published**
   in history — either purge them (`filter-repo`) or make the repo private.
   Same class, still live: `data/` has no vault rule yet for future subtrees.
0b. **Around 2026-08-17, `backup_db.sh` will delete
   `mission-pre-purge-20260802T000402.db`** — the recovery point taken before
   the knowledge-graph purge. Move it out of `data/backups/` or fix the
   retention glob before then.
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
