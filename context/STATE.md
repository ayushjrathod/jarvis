# STATE — read me first each session

_Last updated: 2026-07-05 (session 1, end)_

## Current phase

**Phase A (dispatcher core) — implemented, all acceptance criteria passing.
STOPPED for user review per working-style gate.** Phase B (Jarvis voice
pipeline) starts only after the user signs off on Phase A.

## What exists and works (verified 2026-07-05)

- `dispatcher/` — FastAPI service on :8765: POST /task (quick→SSE stream,
  agentic→202+ack), GET /task/{id}, /tasks, /events (SSE lifecycle feed),
  /health, POST /task/{id}/cancel. Service layer is the single dispatch path.
- Quick path: streamed answer, run row with cost/tokens ("capital of France"
  → Paris, ~$0.04 warm). Backend auto-selects claude_cli (no API creds on
  machine — see CLAUDE.md operational notes).
- Agentic path: headless `claude -p` with allowedTools + budget + timeout;
  summarize acceptance task wrote vault/briefs/test.md, cost $0.39, 5 turns.
- Refusal→fallback: simulated via metadata.simulate_refusal; runs logged
  refused(fable default) → done(claude-opus-4-8); task done.
- Queue watcher: drop .md in queue/ → task; processed files archived with
  task id; answer stored in run row.
- 11 unit tests green (`python -m unittest discover tests`).
- `jarvis/plugins/base.py` — the four approved voice ABCs (Phase B fills in
  engines/).

## Next action

1. User reviews Phase A → sign-off or change requests.
2. Phase B on approval: clone OpenVoiceOS + OpenClaw into references/ (lift
   plugin discipline + streaming TTS loop/sanitizer), refactor
   jarvis/ptt_dictate.py record+transcribe into STTEngine, PTT → wake word →
   VAD, barge-in.

## Open questions for the user

- eleanorkonik "build-a-dashboard" gist: couldn't locate without a URL; queue
  pattern + SQLite pragmas were implemented from the approved design instead.
  If you have the link, drop it in — happy to cross-check and lift the backup
  habits for the nightly-backup timer (Phase E).
- Budgets in config.yaml (quick 1.00 / agentic 3.00 notional USD) — sane?

## Standing decisions this session

- This directory is the monorepo root; git initialized (main branch).
- Deviations from proposed Phase A design (all logged in
  context/sessions/2026-07-05-1.md): no --max-turns (CLI lacks it), dual quick
  backend with auto-detect, heuristic-only classifier with LLM-tiebreak hook.
