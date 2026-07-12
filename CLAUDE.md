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

**STOP for user review at the end of each phase.** Before Phase A code: API schema,
SQLite schema, and the four voice ABC signatures must be approved by the user.

## Hard constraints

- Arch Linux, Wayland, compositor-agnostic. **CPU-only** for all local models.
- One Python venv for backend/voice; Node for the React UI.
- Everything runs as systemd **user** services/timers. No cloud scheduling in v1.
- Voice hotkey capture via **raw evdev** (settled — compositor hotkey APIs don't
  expose keyup, breaking hold-to-talk). Text injection via ydotool — NOT
  installed as of 2026-07-10 (`sudo pacman -S ydotool` + enable the ydotool
  user service); dictation typing fails until then.
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
- **Trigger layout (settled 2026-07-10)**: `mission-jarvis` runs `--mode wake`
  ("hey jarvis" → assistant, spoken reply); `mission-dictate` owns hold-F9
  (speech typed at cursor via ydotool). Both share `trigger_key`/models via
  config.yaml; don't run the assistant in `ptt`/`both` mode while
  mission-dictate is up or F9 will trigger both.

## Known quirks / open items

- **Arch /dev/uinput quirk** (document in setup docs): the udev rule sometimes
  fails to apply and ydotoold won't start. Workaround: `chmod g+rw /dev/uinput`
  and re-`modprobe uinput`.
- **`jarvis/ptt_dictate.py`** — user's original PTT dictation script, saved
  verbatim 2026-07-05. Phase B: refactor its record+transcribe logic into
  engines behind the STTEngine ABC; keep the script alive as standalone
  dictation mode. Its evdev keyboard-watch loop is the reference for hotkey
  capture (needs user in `input` group).
