# Mission Control

Local-first personal "life OS" on Arch Linux: a dispatcher service that owns
every Claude invocation, with **Jarvis** (fully local voice assistant) and a
React dashboard as front-ends. Everything runs on this machine — models on
CPU, no cloud scheduling.

> Dir is named `jarvis/` but it's the monorepo root; the voice code lives in
> the `jarvis/` subdirectory. Project spec + phase tracker: `CLAUDE.md`.
> Current state: `context/STATE.md`.

## Status

| Phase | What | Status |
|---|---|---|
| A | Dispatcher (API, queue, classifier, headless runner, refusal fallback) | ✅ |
| B | Jarvis voice pipeline (PTT, wake word, VAD, TTS, barge-in) | ✅ user-verified |
| C | Tasks area + daily brief + weekly review timers | ✅ |
| D | React dashboard (served by the dispatcher) | ✅ |
| E | systemd services + install script + nightly backup | ✅ — **reboot test pending** |
| F | Memory: episode capture, core blocks, vault index, nightly consolidation | ✅ |
| G | Learning loop: reflection on complex runs, skill telemetry, `/learn` | ✅ |
| H | Observability: run-step timeline, TTFT/latency, `/stats`, dashboard X-ray | ✅ |
| I | Personal OS: pdf/image/html ingest, NL→automations, notify-or-not gate | ✅ |
| J | Semantic + graph memory: vector search, bi-temporal fact graph | ✅ |
| K1 | Desktop control: volume/lock/launch/open/clipboard + confirm gate | ✅ |
| K2 | Windows over AT-SPI + browser tabs over DevTools, zero new deps | ✅ |

Also shipped: **ask about my screen** (`<Super><Alt>a`) · **Spotify control**
by voice · **phone access** over Tailscale + installable PWA · **inbox
watcher** (drop a file in `vault/inbox/`, it indexes itself) · **degraded
mode** (answers from local memory when Claude's plan cap is hit).

**738 tests**, all green. Read `context/STATE.md` for exactly where things
stand; `CLAUDE.md` has the operational notes for every subsystem.

## One-time setup

```bash
cd ~/Documents/code/jarvis

# 1. Python venv (already created; recreate with:)
python -m venv .venv
.venv/bin/pip install fastapi uvicorn pyyaml \
  sounddevice evdev httpx numpy faster-whisper onnxruntime \
  'openwakeword==0.4.0' piper-tts sqlite-vec fastembed pymupdf rapidocr-onnxruntime jeepney

# 2. Voice models (~90MB total; idempotent)
./scripts/setup_voice.sh

# 3. F9 hotkey needs raw input access — then LOG OUT and back in
sudo usermod -aG input $USER

# 4. (dictation only) ydotool types at your cursor; if ydotoold won't start:
sudo chmod g+rw /dev/uinput && sudo modprobe -r uinput && sudo modprobe uinput
```

No API key needed — the dispatcher drives the `claude` CLI with your
subscription login (`CLAUDE_CONFIG_DIR=~/.claude-per`, set in `config.yaml`).
**Costs shown in logs are notional** on that path (you're on subscription): a
quick answer logs ~$0.04–0.14, an agentic task ~$0.25–1.

**There is no API-key path** — an Anthropic Messages API backend existed
briefly and was removed on 2026-07-27. Don't export `ANTHROPIC_API_KEY`: the
`claude` CLI *prefers* a key over your claude.ai login, so a stray one replaces
your subscription rather than supplementing it. The dispatcher strips it from
every `claude` subprocess for exactly that reason.

## Running

**Everything runs as systemd user services now.** One-time install:

```bash
./systemd/install.sh      # dispatcher + dashboard + all timers, auto-restart
loginctl enable-linger $USER   # optional: start at boot without logging in
```

That starts the dispatcher (with the dashboard at **http://127.0.0.1:8765/**)
and schedules: daily brief 07:30, weekly review Sun 18:00, DB backup 03:30
(kept 14 days in `data/backups/`). The backup covers **SQLite only** — not
`vault/`, not `config.yaml`, not `data/spotify.json`, and not `~/.claude-per`. Jarvis voice is installed but disabled
until your mic/input-group setup is done: `systemctl --user enable --now
mission-jarvis`. Logs: `journalctl --user -u mission-dispatcher -f` (same for
`mission-jarvis`, `mission-daily-brief`, …).

### Dashboard

Open **http://127.0.0.1:8765/** — today's brief, task list (checkboxes update
the vault files), live agent monitor, and a command box that behaves exactly
like talking to Jarvis ("add a task: …" works typed).

### Docs

**http://127.0.0.1:8765/system-docs** (the "docs →" link in the dashboard
header) — the full user guide (setup, service control, voice, memory,
automations, config, troubleshooting) plus an HTTP API reference for every
dispatcher route. Content lives in `ui/src/docs/content.js`; edit it and
`npm run build`. FastAPI's generated Swagger UI stays at `/docs`.

### Service control (start / stop / status / logs)

The dispatcher (and the daily-brief / weekly-review / backup timers) runs as
a systemd **user** service — it does not need a terminal open, and restarts
itself on crash.

```bash
systemctl --user start   mission-dispatcher   # start
systemctl --user stop    mission-dispatcher   # stop (also frees port 8765)
systemctl --user restart mission-dispatcher   # apply a config.yaml change
systemctl --user status  mission-dispatcher   # is it up? recent log tail
journalctl --user -u mission-dispatcher -f    # live logs (Ctrl-C to stop watching)
```

Same verbs work for `mission-jarvis`, `mission-daily-brief.timer`,
`mission-weekly-review.timer`, `mission-backup.timer`. `stop` only pauses it
for the current boot; `disable` (`systemctl --user disable mission-dispatcher`)
stops it from auto-starting on future logins, `enable` reverses that. Check
`systemctl --user is-enabled mission-dispatcher` if you're not sure which
state it's in.

**Restart after any change to `config.yaml`** (model, budgets, tool
allowlists, etc.) — the dispatcher reads it once at startup and won't notice
an edit until restarted.

Model in use is whichever is set in `config.yaml`'s `dispatcher.models` block
(quick / agentic / fallback) plus the CLI default in
`~/.claude-per/settings.json` (the `CLAUDE_CONFIG_DIR` the dispatcher execs
`claude` with — separate from your own interactive `claude` config).

### Dispatcher by hand (dev)

```bash
systemctl --user stop mission-dispatcher   # get the port back first
.venv/bin/python -m dispatcher.main        # serves 127.0.0.1:8765
```

Try it without voice:

```bash
# quick question → answer streams back live (SSE)
curl -sN -X POST localhost:8765/task -H 'content-type: application/json' \
  -d '{"text": "what is the capital of France?"}'

# agentic task → instant ack, runs in background
curl -s -X POST localhost:8765/task -H 'content-type: application/json' \
  -d '{"text": "summarize the files in vault/notes into vault/briefs/test.md",
       "metadata": {"task_type": "summarize"}}'

curl -s localhost:8765/tasks | python -m json.tool   # history + costs
curl -sN localhost:8765/events                        # live event feed
```

Or drop a markdown file into `queue/` — it becomes a task within ~5s and the
processed file moves to `queue/.processed/`:

```markdown
---
mode: agentic
---
Summarize this week's notes.
```

### Talking to the tasks area

Voice or command box, same phrases: "add a task: renew the domain by friday" ·
"list my open tasks" · "mark the domain task as done" · "morning brief" (reads
today's brief aloud/streamed). Trigger words match loosely — "can you add a
new task for me" works. Task files live in `vault/tasks/`, briefs in
`vault/briefs/`.

**The daily brief is deliberately email-free** (your call, 2026-08-01). A
read-only Gmail MCP server *is* registered in `~/.claude-per`, so
`mcp__claude_ai_Gmail__*` appears in every agent's tool list — but the brief is
not granted it, and its prompt says explicitly not to call the tools it can see.
(The old dead `mcp__gmail` grant matched nothing, so the agent requested Gmail,
was denied, and burned a turn on every run from Phase C until 2026-08-01.)
To enable it: grant `mcp__claude_ai_Gmail__search_threads` +
`…__get_thread` in `areas/tasks/agents/daily-brief.md` and restore an Email
section to the prompt — no code change needed.

### Memory

Jarvis remembers. Say "remember that …" or just talk to it: every settled
interaction becomes an episode, and a **nightly agent (02:30)** distils the
durable facts into `vault/memory/USER.md` and `MEMORY.md`, which are injected
into every prompt afterwards. Ask "what do you remember about …" to check.
Search is hybrid — keyword **and** meaning (`GET /memory/search?q=…`), over the
whole vault plus a bi-temporal fact graph that supersedes old facts instead of
deleting them. Edit the two block files by hand whenever you like.

### Automations

Say or type a schedule and it becomes a standing job: *"every morning at 8,
tell me the weather"*. One cheap parse, then an in-dispatcher scheduler runs
it. Manage them from the dashboard widget or `GET /automations`. Results are
**gated** — a "notify-or-not" check decides whether a run is worth
interrupting you for, so routine successes stay quiet.

### Ask about my screen

Press **`<Super><Alt>a`**, drag a box, and a small popup opens where you can
type or speak a question about what you just captured; the answer streams back
and follow-ups keep the image in context. One-time: `./scripts/setup_ask_screen.sh`.

### From your phone

`./scripts/setup_tailscale.sh` (after `sudo pacman -S tailscale`) puts the
dashboard on your tailnet over HTTPS — no public port, no password, only your
own devices. Paste the `public_hosts` line it prints into `config.yaml`,
restart, then open the URL on your phone and **Add to Home Screen**: it
installs as an app, mic and all.

### Jarvis by hand (voice, dev)

```bash
.venv/bin/python -m jarvis.main              # PTT + wake word
.venv/bin/python -m jarvis.main --mode ptt   # push-to-talk only
.venv/bin/python -m jarvis.main --mode wake  # wake word only
```

**What to expect:**

- Startup loads all models warm — ~3–5s, then "jarvis is up".
- **Two triggers, both reach the assistant.** **Wake:** say **"hey jarvis"**,
  pause, speak; it stops listening after ~0.9s of silence. **Push-to-talk:**
  hold **Right Ctrl**, speak, release (a beep marks each edge; no silence
  detection needed — the key release *is* the endpoint).
- **F9 is dictation, not the assistant** — hold it and your speech is typed at
  the cursor (`mission-dictate`). The two keys must differ: both services read
  the raw evdev stream, so sharing one would fire both at once.
- Quick questions: answer is spoken sentence-by-sentence as it streams.
  First audio ~4–6s after you finish speaking (~1s transcription + ~3–5s to
  first model output — the floor on the subscription CLI path).
- Long/agentic asks ("summarize my notes…"): you hear **"On it — …"**
  immediately, then a spoken **"Done: …"** when it finishes minutes later.
- **Barge-in:** talk over Jarvis (or press Right Ctrl) and it shuts up within
  ~200ms and aborts the request. ⚠️ With speakers the mic hears Jarvis itself —
  use headphones, or: `pactl load-module module-echo-cancel` and select the
  echo-cancel source.
- **Music:** "play <song>", "pause", "skip", "what's playing", "volume 40" go
  straight to the local Spotify client — no model, no cost, under a second.
  Song lookup by name needs `./scripts/setup_spotify.sh` once (free app
  registration; **no Premium required**); transport works without it.
- **The desktop:** "lock the screen", "open firefox", "system volume 40",
  "what's on my clipboard" — also deterministic. **"open \<app>" works for any
  installed app** — names are matched against your `.desktop` entries ("open
  text editor", "launch obsidian"), and an app that doesn't start is reported
  as such rather than claimed as opened. Apps run in their own systemd scope,
  so restarting the dispatcher never closes them. Reading the clipboard and
  opening a URL ask first ("Shall I …?"); answer by saying **yes/no**, or click
  the banner at the top of the dashboard. Windows list and focus over AT-SPI
  ("what windows are open", "switch to firefox"); browser tabs list, switch
  and read over the DevTools port ("what tabs are open", "read this tab") —
  the browser must run with `--remote-debugging-port`.
- **Rate limited?** If Claude's plan-wide session cap is hit, Jarvis says so
  *and* answers from its own indexed memory where it can — quoting the vault
  verbatim with the source named, rather than going silent until the reset.

### Dictation mode (your original workflow)

```bash
.venv/bin/python -m jarvis.dictate   # hold F9 → speak → types at cursor
```

(Original script kept at `jarvis/ptt_dictate.py`, untouched.)

## Tests

```bash
.venv/bin/python -m unittest discover tests   # 738 unit tests, no network, ~6s
./scripts/smoke_phase_a.sh                    # dispatcher acceptance (server must be up)
.venv/bin/python scripts/smoke_phase_b.py     # voice acceptance, no mic needed
./scripts/smoke_ask_screen.sh                 # ask-about-my-screen acceptance
./scripts/doctor.sh                           # read-only health check
```

## Configuration — `config.yaml`

Single file for everything: models (quick/agentic/fallback), budgets and
timeouts, per-task-type tool allowlists, trigger key, STT/TTS engine + voice,
VAD/barge-in tuning. Engines are swappable one-line (e.g. a future Kokoro TTS:
`tts: {engine: kokoro}`).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `no readable keyboard devices` | Not in `input` group, or didn't re-login |
| ydotoold dead / dictation types nothing | `/dev/uinput` quirk — see setup step 4 |
| Wake word never fires | Check mic is the default source (`wpctl status`); lower `wake_threshold` in config.yaml |
| Jarvis interrupts itself while speaking | Echo — headphones or PipeWire echo-cancel |
| Agentic task status `failed`, error `error_max_budget_usd` | Raise `budgets.max_cost_per_task_usd` (a bare `claude -p` already "costs" ~$0.15–0.25 notional) |
| Quick answers slow | ~3s to first word is the floor: every cold `claude -p` re-sends the Claude Code system prompt (~16–18k tokens) |
| Every quick answer suddenly fails | Check for a stray `ANTHROPIC_API_KEY` in the environment — it makes the CLI abandon your subscription login |
| Jarvis answers but never speaks scheduled results | By design — routine timer/automation successes are gated (`notify-or-not`) and only interesting ones are announced |
| A file in `vault/inbox/` isn't searchable | Give it ~60s (the watcher waits for the file to stop changing), then check `journalctl --user -u mission-dispatcher \| grep inbox` |
| Desktop verb says "I'm not allowed to…" | Its policy is `deny` in `config.yaml`'s `computer.policy` block; `GET /desktop/verbs` shows all of them |
| "\<app> didn't start" / "I can't reach your desktop session" | The dispatcher was started before your graphical session, so it has no display to launch into. It borrows one from the systemd user manager — `./scripts/doctor.sh` says whether the manager has one; if not, log into the desktop and `systemctl --user restart mission-dispatcher` |

## Layout

```
dispatcher/   FastAPI service — the ONLY thing that calls Claude
jarvis/       voice: plugins/ (ABCs), engines/, main.py, dictate.py, ask_screen.py
areas/        life areas as skills (tasks/, memory/, learn/) — one dir each
ui/           React SPA + PWA (src/, public/, dist/ is committed and served)
vault/        markdown memory (notes/, tasks/, briefs/, memory/, inbox/)
queue/        drop-a-.md-file task queue
data/         SQLite (mission.db), backups, voice + embedding models
systemd/      user units and timers + install.sh
scripts/      setup_*.sh, smoke tests, doctor.sh
context/      STATE.md + session logs (read STATE.md first, always)
future/       researched-but-unbuilt proposals (computer use, feature gaps)
references/   cloned reference repos (gitignored)
```
