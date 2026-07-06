# Reference-Repo Adaptation Audit — 2026-07-06

## Approval status (2026-07-06, session 3)

User approved items 1-4 for immediate implementation, one commit each, tests
per item. All four are **done**:

- **1. Queue watcher retry-on-transient-failure** — implemented in
  `dispatcher/queue_watcher.py` (`_ingest_one`), `queue_max_retries` config.
  Commit f3f3d12.
- **2. Runner transient-error classification + backoff** — implemented in
  `dispatcher/runner.py`. Per user rider: retries **exactly once**, whitelist
  is **timeout + spawn (OSError) only** — refusal and budget-exceeded are
  never retried here (unchanged, handled by the existing model-fallback
  loop). `transient_retry_delay_s` config. Commit 6bb489f.
- **3. `scripts/doctor.sh`** — implemented, read-only diagnostics (venv,
  config, uinput/ydotoold, input group, voice models, dispatcher reachability,
  timers). Commit 45518a0.
- **4. Pre-wake-word audio ring buffer** — implemented in new
  `jarvis/wake_capture.py` (`capture_after_wake`), wired into
  `jarvis/main.py`. Per user rider: buffer size is `wake_prebuffer_ms` in
  `config.yaml` (default 400ms). Acceptance per user's stated bar (synthetic
  Piper test proving the first word survives) run via
  `scripts/smoke_phase_b.py`: synthesized "Hey Jarvis, what time is it right
  now" transcribes in full with the buffer on (43520 samples captured vs
  37376 unbuffered) — passing live, not just unit-tested. Commit d018153.

**Item 5** (stream agentic runs for dashboard visibility) — approved in
principle, **deferred**; not started this session, to be scoped as its own
task per the original recommendation.

**Items 6 and 7** (adaptive endpointing; decision/commitment journal) —
**skipped / parked** per user decision. Not built. Revisit only if the user
explicitly reopens locked decision #7 (item 6) or explicitly requests a new
life area (item 7).

Scope: what we *missed* from `references/` (updated via `git pull`) plus two newly
cloned repos, matched against real weak spots in our five phases. No code
changed by this audit — report only.

## Step 1 — inventory (weak spots found)

- `dispatcher/queue_watcher.py`: any exception during ingest (including a
  transient one — dispatcher briefly overloaded, disk hiccup) sends the file
  straight to `.processed/.failed` with **no retry**. No terminal-vs-transient
  distinction.
- `dispatcher/runner.py`: `run_once` treats every non-JSON-parseable /
  non-refusal failure as terminal. No retry/backoff for transient subprocess
  or exec errors (only the refusal path gets a second attempt, on a different
  model).
- `jarvis/main.py` `_wake_capture_once`: frames are appended to the utterance
  buffer only *after* `self.wake.process(frame)` returns true — nothing is
  captured from before the wake word fires, so audio in the same buffer chunk
  as the trigger can clip the first word.
- `ui/` AgentMonitor widget + dispatcher `events.py`/`hooks.py`: SSE events are
  task-level only (`queued`/`started`/`done`/`failed`). Agentic runs use
  `--output-format json` (batched final output) — no mid-run visibility into
  tool calls while a `claude -p` run is in flight.
- No project-local diagnostic script. The "Known quirks" section of
  `CLAUDE.md` (uinput chmod, input group, PipeWire echo-cancel, reboot test)
  is all manual triage; nothing scripts a first-look health check.
- `areas/tasks/` has no "decision" or "commitment" concept — tasks are
  either open/done, no way to track a promise made in conversation ("I'll
  follow up on X by Friday") without the user explicitly saying "add a task."

## Quick wins (S effort, low risk)

1. **Queue watcher: retry transient failures instead of failing on first
   error**
   - What: OpenClaw's `src/commitments/runtime.ts` (`drainCommitmentExtractionQueue`,
     `scheduleDrainSoon`) — single-slot debounced retry: on a non-terminal
     error the batch is `unshift`ed back to the queue front and a drain is
     rescheduled; only a classified *terminal* error (bad auth/model) drops
     the item permanently.
   - Where it lands: `dispatcher/queue_watcher.py`.
   - Why: today a transient failure (dispatcher momentarily busy, a locked
     file) permanently moves the queue file to `.failed` — same bug class
     this pattern was built to fix.
   - License: MIT (OpenClaw). Reimplement the pattern (our queue shape is a
     directory of files, not an in-memory array); no direct code lift.
   - Risk: low — only touches the failure branch of an already-isolated loop.
   - Verdict: **adopt**. Add one classifier (`is_transient(exc)`) and re-stage
     the file instead of moving it to `.failed` on transient errors, capped at
     N retries.

2. **Runner: classify transient vs terminal subprocess/exec errors, retry
   once with backoff**
   - What: `src/provider-runtime/operation-retry.ts`
     (`isTransientProviderOperationError`, `resolveTransientProviderDelayMs`)
     — status-code/error-message based transient classification + capped
     exponential backoff with jitter.
   - Where it lands: `dispatcher/runner.py` `run_once`.
   - Why: right now any spawn/exec failure (not just refusal) is terminal on
     the first try. A blip (e.g. transient `ENOENT` during a filesystem hiccup,
     a momentarily unavailable CLI binary) burns the whole task instead of
     one retry.
   - License: MIT — pattern only (their classifier is HTTP-status-shaped;
     ours needs to key off subprocess return code / stderr text).
   - Risk: low-medium — must not retry actual refusals or budget-exceeded
     errors (those are already handled deliberately elsewhere); keep retry
     scope narrow (timeout/spawn errors only).
   - Verdict: **adopt**, narrow scope.

3. **`scripts/doctor.sh`-style health check**
   - What: `references/agentic-os/hermes/doctor.sh` — checks prereqs, config
     validity, state dir contents, and reports ✓/!/✗ per item.
   - Where it lands: new `scripts/doctor.sh` (or `.py`) at repo root.
   - Why: our own "Known quirks" section is a list of manual checks a human
     has to remember (uinput permissions, input group membership, ydotoold
     running, dispatcher reachable on :8765, config.yaml parses, silero/openww
     model files present). A single script surfaces all of it at once —
     directly useful for the still-open "reboot test" and any future machine
     setup.
   - License: MIT (aporb/agentic-os) — read-only shell script, safe to adapt
     structure/style, no code shares logic worth lifting verbatim (their
     checks are Node/vault-specific).
   - Risk: none — purely additive, read-only diagnostics.
   - Verdict: **adopt**.

4. **Pre-wake-word audio buffering (avoid clipping the first word)**
   - What: general LiveKit Agents / Pipecat pattern — keep a small rolling
     ring buffer of the last ~300-500ms of mic audio continuously; when the
     wake word (or PTT) fires, splice the buffer onto the front of the
     captured utterance instead of starting capture from zero.
   - Where it lands: `jarvis/main.py` `_wake_capture_once` /
     `jarvis/audio.py` `MicStream`.
   - Why: today frames only enter the utterance buffer after
     `self.wake.process(frame)` returns true; whatever the user said in the
     same ~80ms detection window is lost. A short rolling buffer fixes this
     without touching the barge-in path.
   - License: pattern only, no repo cloned (LiveKit/Pipecat weren't pulled
     in — reference by pattern per the task brief).
   - Risk: low — additive to `MicStream`, does not touch the barge-in
     interrupt logic (`Player`/`speak_sentences`), so the <200ms barge-in
     target is unaffected.
   - Verdict: **adopt**.

## Worth planning (M/L effort)

5. **Stream agentic runs for mid-run dashboard visibility**
   - What: pattern from `Claude-Code-Agent-Monitor`'s
     `hooks -> API -> SQLite -> WebSocket -> UI` pipeline (`server/routes/sessions.js`)
     — it surfaces per-line transcript events (including tool calls) live,
     not just start/end.
   - Where it lands: `dispatcher/runner.py` (switch agentic runs from
     `--output-format json` to `--output-format stream-json`), `dispatcher/hooks.py`
     / `events.py` (new event types for tool-call progress), `ui/` AgentMonitor
     widget.
   - Why: our dashboard's AgentMonitor currently only shows queued → running →
     done/failed; a multi-minute agentic task (e.g. weekly review) gives no
     mid-run feedback at all.
   - License: MIT (hoangsonww) — architecture pattern, not code (their stack
     is Node/Express, ours is FastAPI/asyncio).
   - Effort: M — `run_once` currently reads one final JSON blob
     (`_parse_json`); switching to streaming NDJSON parsing changes its
     control flow, and the cancel path (`procs[task_id]`) needs to keep
     working against a long-lived stdout reader instead of `communicate()`.
   - Risk: medium — touches the same runner used for every scheduled agent
     (daily brief, weekly review) and the cancel/kill path; needs its own
     regression tests before rollout, not a quick patch.
   - Verdict: **adapt** — good value, but should be its own scoped task with
     tests, not bundled into a quick win.

6. **Adaptive endpointing for mid-sentence pauses**
   - What: LiveKit/Pipecat endpointing refinements — treat a pause after
     clearly-incomplete speech (trailing conjunction, rising intonation, no
     terminal punctuation) differently from a pause after a complete
     sentence, instead of one fixed silence timeout.
   - Where it lands: `jarvis/main.py` `_wake_capture_once` (fixed
     `endpoint_silence_ms` / `silence_limit`).
   - Why: real gap — a user pausing mid-sentence ("add a task... to renew, uh,
     the domain") can get cut off by the fixed silence window.
   - License: pattern only, no dependency.
   - Effort: L — meaningful adaptive endpointing needs streaming partial
     transcripts to detect "incomplete" phrasing; our STT
     (`faster-whisper`) only returns a result after the recording ends, so
     this requires switching to a streaming-capable STT call pattern first.
   - Risk: high for the payoff — this sits directly in the barge-in-adjacent
     hot path (`CLAUDE.md` locked decision #7: "plain VAD endpointing — no
     semantic turn detection" is explicitly locked in). Recommend **skip
     unless the user wants to revisit locked decision #7** — flagging as
     worth-planning only because it's a real limitation, not because it's
     currently in scope.

7. **Decision/commitment journal with revisit dates**
   - What: `references/lifeos-template`'s `/decide` + `/decide-revisit` skills
     — a decision is captured prospectively (what you predict, why, a revisit
     date), then a monthly skill walks through decisions whose date has
     passed and compares prediction vs. reality. Conceptually close to
     OpenClaw's `src/commitments/*` (extract "I'll get back to you on X"-style
     promises from conversation into a tracked, revisitable list).
   - Where it lands: would be a **new life area** (`areas/decisions/`), not a
     patch to `areas/tasks/`.
   - Why: real gap — nothing today tracks an open-ended commitment or
     decision that isn't phrased as an explicit "add a task."
   - License: lifeos-template is CC BY 4.0 (attribution required, no code to
     lift — it's a skill/prompt, not implementation); OpenClaw's commitments
     extractor is MIT but deeply wired into their agent-turn lifecycle —
     pattern-reimplementation only either way.
   - Effort: M for a minimal version (a SKILL.md + vault convention, no core
     dispatcher changes needed since areas are already plugins).
   - Risk: low technically, but **this is a new life area**, which
     `CLAUDE.md`'s Deferred list explicitly puts out of scope right now
     ("other life areas... do not build unprompted").
   - Verdict: **adapt, but only on explicit request** — flagging the pattern
     now so it's ready to pull off the shelf later; not proposing to build it
     unprompted.

## Looked promising but skip

- **OVOS engine-fallback / pipeline-crash-recovery** (`ovos-dinkum-listener`,
  `ovos-audio`) — the actual crash-recovery and audio-ducking code lives in
  those repos, not in `ovos-plugin-manager` (the only OVOS repo we have
  cloned). What *is* in `ovos-plugin-manager` (`templates/stt.py`'s
  `RuntimeRequirements` capability descriptor for online/offline fallback
  declaration) doesn't apply: all four of our voice engines are
  offline/CPU-only by hard constraint, so there's no "try local then remote"
  fallback chain to declare. Skip — no real gap, and pulling in the larger
  OVOS repos to check would violate the "no speculative features" rule.
- **OpenClaw heartbeat-ack suppression**
  (`src/auto-reply/heartbeat.ts`/`heartbeat-filter.ts`) — filters
  low-content "no updates" acks out of delivery. We don't have a heartbeat
  noise problem: our daily-brief/weekly-review timers fire once a day/week
  and always have real content. Skip — solves a problem we don't have.
- **Command-poll-backoff exponential schedule**
  (`src/agents/command-poll-backoff.ts`) — a client-side polling backoff for
  checking on a long-running command's output. We don't poll; the dispatcher
  pushes over SSE already (`dispatcher/events.py`), which is strictly better
  than what this pattern retrofits. Skip.
- **Barge-in interruption-resume (replay the cut-off sentence)** — Pipecat/LiveKit
  support both "drop" and "replay" strategies on interruption. We already do
  "drop" (`jarvis/main.py` `speak_sentences` calls `self.brain.cancel()` and
  the queue is discarded) — that's a deliberate, working choice for a
  single-user local assistant where the user just interrupted on purpose.
  Adding replay logic would add real complexity to the barge-in hot path
  (<200ms target) for a case (user wants to hear the rest of what got cut
  off) that hasn't come up. Skip unless the user reports wanting it.
- **`local-life-manager`'s `/process-inbox`** — assumes a separate inbox
  folder distinct from final storage. Our voice-capture flow writes directly
  to `vault/tasks/*.md` with no separate capture stage, so there's no inbox
  to triage. Skip — solved differently already, not missing.
- **`lifeos-template`'s `now.md` / `plan.md` snapshot files** — this is
  functionally what `context/STATE.md` + the phase tracker in `CLAUDE.md`
  already do. Skip — duplicate of an existing convention, not a gap.

## Ranked summary

| # | Item | Effort | Risk | Verdict |
|---|------|--------|------|---------|
| 1 | Queue watcher retry-on-transient-failure | S | low | **implemented** (f3f3d12) |
| 2 | Runner transient-error classification + backoff | S | low-med | **implemented**, whitelist narrowed to timeout+spawn only, retries exactly once (6bb489f) |
| 3 | `scripts/doctor.sh` health check | S | none | **implemented** (45518a0) |
| 4 | Pre-wake-word audio ring buffer | S | low | **implemented**, verified live via smoke_phase_b.py (d018153) |
| 5 | Stream agentic runs for dashboard visibility | M | medium | approved in principle, **deferred** — own future task |
| 6 | Adaptive endpointing | L | high | **skipped/parked** per user — do not build |
| 7 | Decision/commitment journal | M | low (but new life area) | **skipped/parked** per user — do not build |
| — | OVOS pipeline crash-recovery/ducking | — | — | skip (wrong repo, no real gap) |
| — | Heartbeat-ack suppression | — | — | skip (no noise problem) |
| — | Command-poll-backoff | — | — | skip (we push, don't poll) |
| — | Barge-in replay-on-interrupt | — | — | skip (current drop behavior is a deliberate working choice) |
| — | `/process-inbox` | — | — | skip (no inbox stage exists) |
| — | `now.md`/`plan.md` snapshot | — | — | skip (duplicate of STATE.md) |

New references cloned into `references/`: `agentic-os` (MIT),
`lifeos-template` (CC BY 4.0).
