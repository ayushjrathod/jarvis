# STATE — read me first each session

_Last updated: 2026-07-10 (session 7, end)_

## Current phase

**ALL FIVE PHASES BUILT AND VERIFIED.** v1 is feature-complete per the spec.
The system runs under systemd user services. Awaiting the user's end-to-end
hardware test (their stated plan) and reboot test.

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
