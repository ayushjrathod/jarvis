# STATE — read me first each session

_Last updated: 2026-07-06 (session 1, end)_

## Current phase

**Phase B (Jarvis voice pipeline) — implemented, headless acceptance 10/10.
STOPPED per user instruction ("do phase B and stop").** User granted autonomy
through the remaining phases earlier but then scoped this session to B; get a
go-ahead before starting Phase C. User will hardware-test everything at the end.

## What exists and works

- Phase A dispatcher (see 2026-07-05 log) — unchanged, still passing.
- Phase B voice pipeline: engines behind the four ABCs (faster-whisper,
  openwakeword 0.4.0, Piper, DispatcherBrain), VAD utility, audio I/O with
  interruptible player, evdev hotkey, sanitizer+chunker, main loop (PTT + wake
  + barge-in), dictation mode. 24 unit tests green; smoke_phase_b.py 10/10
  against a live dispatcher (TTS-synthesized speech drives wake/VAD/STT).
- Voice models cached under data/models/ (silero_vad.onnx, piper voice) and
  in-package (hey_jarvis) / HF cache (whisper small.en).

## Hardware still untested (user, at the end)

Mic capture, F9 hold (needs `input` group), speakers, real barge-in (echo
caveat: use headphones or PipeWire echo-cancel — noted in CLAUDE.md).

## Next action

1. User go-ahead → Phase C: areas/tasks skill, area-aware agentic dispatch
   (inject area SKILL.md into claude -p runs), task capture into vault/tasks/,
   daily-brief + weekly-review agents, systemd user timers. Gmail MCP optional
   (needs user OAuth; brief must degrade gracefully without it).
2. Then Phase D (React dashboard over /events + /task), Phase E (systemd).

## Open questions for the user

- eleanorkonik build-a-dashboard gist URL still unknown (non-blocking).
- Phase C daily-brief timer time-of-day preference (defaulting to 07:30 local
  unless told otherwise).

## Standing decisions

- Monorepo root = this dir; git on main. openwakeword pinned 0.4.0 (Py 3.14 /
  tflite); Silero VAD context-sample fix in jarvis/vad.py; wake reset flushes
  zeros — details in CLAUDE.md operational notes + session logs.
