# STATE — read me first each session

_Last updated: 2026-07-05 (session 1)_

## Current phase

**Phase A (dispatcher core) — design proposed, awaiting user approval.**
No implementation code written yet (per working-style gate: API schema, SQLite
schema, and the four voice ABCs must be approved first).

## What exists

- Repo skeleton (all directories from the layout in CLAUDE.md), CLAUDE.md,
  .gitignore, this context system. Nothing else — the repo started empty.
- Proposed Phase A design: see `context/sessions/2026-07-05-1.md` (full artifacts
  were presented in chat; session log has the summary + key choices).

## Next action

1. Get user verdict on the Phase A design (API schema, SQLite schema, voice ABCs,
   config sketch, classifier approach).
2. On approval: clone `eleanorkonik` gist + OpenVoiceOS into `references/`, set up
   the Python venv (FastAPI, uvicorn, pyyaml, anthropic — ask-before-add applies
   to anything beyond these), then implement dispatcher per approved design.

## Open questions for the user

- ~~`ptt_dictate.py` location~~ RESOLVED: user pasted it; saved verbatim at
  `jarvis/ptt_dictate.py`. Refactor into STTEngine happens in Phase B.
- Repo is not a git repository. `git init`? (Recommended before Phase A code.)

## Standing decisions this session

- This directory (`~/Documents/code/jarvis`) is the monorepo root, despite the
  spec calling it `mission-control/`. Voice pipeline lives in `jarvis/` subdir.
