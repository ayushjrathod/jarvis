# Mission Control

Local-first personal "life OS" on Arch Linux: a dispatcher service that owns
every Claude invocation, with **Jarvis** (fully local voice assistant) and a
React dashboard (Phase D, not built yet) as front-ends. Everything runs on
this machine — models on CPU, no cloud scheduling.

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

## One-time setup

```bash
cd ~/Documents/code/jarvis

# 1. Python venv (already created; recreate with:)
python -m venv .venv
.venv/bin/pip install fastapi uvicorn pyyaml anthropic \
  sounddevice evdev httpx numpy faster-whisper onnxruntime \
  openwakeword piper-tts

# 2. Voice models (~90MB total; idempotent)
./scripts/setup_voice.sh

# 3. F9 hotkey needs raw input access — then LOG OUT and back in
sudo usermod -aG input $USER

# 4. (dictation only) ydotool types at your cursor; if ydotoold won't start:
sudo chmod g+rw /dev/uinput && sudo modprobe -r uinput && sudo modprobe uinput
```

No API key needed — the dispatcher drives the `claude` CLI with your
subscription login (`CLAUDE_CONFIG_DIR=~/.claude-per`, set in `config.yaml`).
If you ever export `ANTHROPIC_API_KEY`, quick answers automatically switch to
the lower-latency Messages API. **Costs shown in logs are notional** (you're
on subscription): a quick answer logs ~$0.04–0.10, an agentic task ~$0.40–1.

## Running

**Everything runs as systemd user services now.** One-time install:

```bash
./systemd/install.sh      # dispatcher + dashboard + all timers, auto-restart
loginctl enable-linger $USER   # optional: start at boot without logging in
```

That starts the dispatcher (with the dashboard at **http://127.0.0.1:8765/**)
and schedules: daily brief 07:30, weekly review Sun 18:00, DB backup 03:30
(kept 14 days in `data/backups/`). Jarvis voice is installed but disabled
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

**Gmail in the daily brief (optional):** register a read-only Gmail MCP server
in Claude Code (`claude-per mcp add gmail …` + OAuth). The brief agent uses it
automatically when present and silently skips email when not.

### Jarvis by hand (voice, dev)

```bash
.venv/bin/python -m jarvis.main              # PTT + wake word
.venv/bin/python -m jarvis.main --mode ptt   # push-to-talk only
.venv/bin/python -m jarvis.main --mode wake  # wake word only
```

**What to expect:**

- Startup loads all models warm — ~3–5s, then "jarvis is up".
- **PTT:** hold **F9**, speak, release. **Wake:** say **"hey jarvis"**, pause,
  speak; it stops listening after ~0.9s of silence.
- Quick questions: answer is spoken sentence-by-sentence as it streams.
  First audio ~4–6s after you finish speaking (CLI backend; ~1s transcription
  + ~3–5s to first model output). An API key would roughly halve this.
- Long/agentic asks ("summarize my notes…"): you hear **"On it — …"**
  immediately, then a spoken **"Done: …"** when it finishes minutes later.
- **Barge-in:** talk over Jarvis and it shuts up within ~200ms and aborts the
  request. ⚠️ With speakers the mic hears Jarvis itself — use headphones, or:
  `pactl load-module module-echo-cancel` and select the echo-cancel source.

### Dictation mode (your original workflow)

```bash
.venv/bin/python -m jarvis.dictate   # hold F9 → speak → types at cursor
```

(Original script kept at `jarvis/ptt_dictate.py`, untouched.)

## Tests

```bash
.venv/bin/python -m unittest discover tests   # 31 unit tests, no network
./scripts/smoke_phase_a.sh                    # dispatcher acceptance (server must be up)
.venv/bin/python scripts/smoke_phase_b.py     # voice acceptance, no mic needed
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
| Quick answers slow | Normal on the CLI backend; export `ANTHROPIC_API_KEY` for the fast path |

## Layout

```
dispatcher/   FastAPI service — the ONLY thing that calls Claude
jarvis/       voice: plugins/ (ABCs), engines/, main.py, dictate.py
vault/        markdown memory (notes/, tasks/, briefs/)
queue/        drop-a-.md-file task queue
data/         SQLite (mission.db) + voice models
scripts/      setup_voice.sh, smoke tests
context/      STATE.md + session logs (read STATE.md first, always)
references/   cloned reference repos (gitignored)
```
