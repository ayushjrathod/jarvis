# Mission Control — how it actually works

A ground-up explanation of the system in this repo, written for the person who
owns it. `README.md` tells you how to run it; `CLAUDE.md` is the working spec
and the accumulated field notes; `context/STATE.md` is the session diary. This
file is the one that answers **"why is it built this way, and what happens when
I speak?"**

Written 2026-08-02 against 492 passing tests, ~14.8k lines of Python, 32 HTTP
route handlers over 31 paths, and 16 systemd units.

---

## 1. The one-sentence version

**Everything that talks to Claude goes through one Python process.** Voice, the
web dashboard, your phone, a markdown file dropped in a folder, and six
systemd timers are all just clients of a single FastAPI service on
`127.0.0.1:8765` that owns every `claude` subprocess, every SQLite write, and
every decision about whether a model needs to be involved at all.

That last clause is the part most people miss. A large fraction of this
codebase exists specifically to **avoid** calling a model.

---

## 2. The mental model: four rings

Think of it as concentric rings, cheapest first. A request falls through them
until one catches it.

```
        ┌─────────────────────────────────────────────────────┐
        │ Ring 4 — AGENTIC   claude -p, tools, minutes, $0.30 │
        │  ┌───────────────────────────────────────────────┐  │
        │  │ Ring 3 — QUICK   claude -p stream, ~3s, $0.14  │  │
        │  │  ┌─────────────────────────────────────────┐  │  │
        │  │  │ Ring 2 — META  haiku classify, $0.04    │  │  │
        │  │  │  ┌───────────────────────────────────┐  │  │  │
        │  │  │  │ Ring 1 — DIVERT  pure Python,     │  │  │  │
        │  │  │  │  <1s, $0.00, no model at all      │  │  │  │
        │  │  │  └───────────────────────────────────┘  │  │  │
        │  │  └─────────────────────────────────────────┘  │  │
        │  └───────────────────────────────────────────────┘  │
        └─────────────────────────────────────────────────────┘
```

- **Ring 1 — diverts.** `spotify.detect`, `desktop.detect`, `automations.detect`,
  `brief.is_quiet`, the inbox watcher. Regexes and file stats. "play jazz",
  "lock the screen", "system volume 40", "every morning tell me X" and a daily
  brief on an empty vault never reach a model. Free, instant, and — importantly
  — **unable to hallucinate**.
- **Ring 2 — meta tasks.** Jobs that are a *classification*, not prose: should
  this result interrupt the user (`notify-gate`), what schedule did they mean
  (`automation-parse`), what search words (`media-parse`). Routed to
  `models.meta` (haiku) because each has mechanical validation downstream, so a
  small model is safe. Measured: **$0.0405 vs $0.189** per run.
- **Ring 3 — quick.** A streamed conversational answer. `claude -p
  --output-format stream-json --include-partial-messages`, deltas relayed as SSE
  as they arrive so TTS can start speaking mid-sentence.
- **Ring 4 — agentic.** `claude -p` headless with a scoped `--allowedTools`, a
  budget cap and a wall-clock timeout. Writes files, runs for minutes, produces
  a run timeline.

The economics behind the rings are the single most load-bearing fact in this
repo, so it gets its own section (§9).

---

## 3. The seven locked decisions, and why

These are in `CLAUDE.md` marked do-not-re-evaluate. Here's what each actually
buys you.

**1. Single dispatch path.** The dispatcher is the only thing that invokes
Claude. Everything else is a client. This is why cost accounting, cancellation,
memory injection, the trust boundary and episode capture each exist *once*
instead of five times. When you added Tailscale phone access, zero dispatch code
changed. When the Messages API backend was deleted, only `quick.py` changed.

**2. Quick vs agentic routing.** `dispatcher/classifier.py:36` — pure heuristic,
~50 lines, has never called a model. Action verbs or a file path ⇒ agentic;
question-shaped or ≤9 words ⇒ quick. There's an `llm_tiebreak` parameter as a
seam, wired to nothing. It leaves no ambiguous bucket, which is why the seam was
never needed.

**3. Refusal fallback.** A `stop_reason: refusal` retries once on
`models.fallback` (opus-4-8), and **both attempts are separate rows** in the
`runs` table. That's what makes the decision auditable rather than a claim —
`SELECT * FROM runs WHERE task_id=?` shows you attempt 1 refused, attempt 2
succeeded.

**4. Memory = markdown vault + SQLite.** No MCP server for your own files; the
agent reads the filesystem natively. SQLite for anything structured. §7 is the
whole picture.

**5. Areas as plugins.** A life area is a directory with a `SKILL.md`. Adding
one requires zero core changes. `dispatcher/areas.py` loads them fresh on every
`.load()` call — no caching, so editing a SKILL.md takes effect on the next
request with no restart.

**6. Voice plugin ABCs.** Four abstract classes in `jarvis/plugins/base.py`:
`WakeWordEngine`, `STTEngine`, `Brain`, `TTSEngine`. Engines are *pure
converters* — playback, sanitization and cancellation deliberately live in the
main loop, so swapping Piper for Kokoro stays a one-line config change.

**7. Barge-in.** While Jarvis speaks, a **second** Silero VAD instance watches
the mic. 6 consecutive 32ms speech frames (~192ms) stops playback *and* cancels
the in-flight Claude call. Measured at 193ms against a 200ms budget.

---

## 4. Walkthrough A: you say "hey jarvis, what's the capital of Peru?"

This is the hot path. Follow it and you've understood 60% of the system.

```
mic ──► openWakeWord ──► Silero VAD ──► faster-whisper ──► HTTP POST /task
                                                                 │
                                                                 ▼
                                                    try_divert()  ── no match
                                                                 │
                                                          classify() → quick
                                                                 │
                                                    stream_quick() ──► claude -p
                                                                 │
                                  SSE deltas ◄──────────────────┘
                                       │
                       SentenceChunker ─► Piper TTS ─► speaker
                                       │
                            (Silero VAD watching for barge-in)
```

**1. Wake.** `jarvis/main.py:292` `_wake_capture_once` opens a `MicStream` and
feeds 32ms frames to openWakeWord (pinned at 0.4.0 — 0.5+ needs tflite-runtime
with no Python 3.14 wheels). Threshold is `0.2`, tuned from real near-miss logs
where your natural "hey jarvis" scores 0.21–0.25. On a hit, a **pre-synthesized**
"Hmm?" plays instantly — pre-synthesized at startup so there's no TTS latency in
the acknowledgment.

Two gotchas encoded here: openwakeword's `reset()` doesn't clear the mel-spec
buffer, so the engine flushes 2s of zeros instead (else stale audio re-fires the
wake word); and a `wake_prebuffer_ms: 400` rolling buffer is spliced onto the
front of the capture, because people start their sentence before the wake word
finishes.

**2. Endpoint.** Silero VAD v5 ONNX needs **64 context samples prepended** to
each 512-sample frame (`jarvis/vad.py` handles it) — bare 512 frames give
near-zero probabilities. Capture ends after `endpoint_silence_ms: 900` of
trailing silence.

**3. Transcribe.** faster-whisper `small.en` int8 on CPU, warm since startup.
~1.1s for a short utterance.

**4. Submit.** `jarvis/engines/brain_dispatcher.py` POSTs to `/task` with
`source: "voice"`. It branches on **HTTP status before content-type** — FastAPI
errors are `application/json` too, and the old check spoke "On it." to a 500 and
then did nothing.

**5. Divert check.** `dispatcher/service.py:249` `try_divert()` runs three
parsers in a load-bearing order: automation → media → desktop. Automation first
so "every morning play jazz" gets *scheduled* rather than played once; media
before desktop so "play …" stays with Spotify rather than being read as an app
launch. None match here, so we continue.

**6. Route.** `route()` → `classify()` → `"quick"`. No area triggers match.

**7. Stream.** `dispatcher/service.py:326` `stream_quick()` is the most
intricate function in the codebase. It:
- prepends the memory blocks (`_with_memory` → `memory.blocks_context`) —
  USER.md and MEMORY.md, budget-clipped, in a `<memory_blocks>` section;
- looks up `quick_sessions["voice"]` for a session to `--resume` (follow-ups
  within `quick_session_idle_minutes: 10` keep context);
- spawns `claude -p` via `quick._stream_cli`, registering the subprocess in
  `svc.procs[task_id]` so a barge-in can kill it;
- yields `("delta", text)` per token, `("done", {...})` at the end;
- records TTFT / ITL-p95 / tokens-per-second into the `runs` row.

**8. Speak.** `jarvis/main.py:191` `handle_utterance` feeds deltas into a
`SentenceChunker`, which emits complete sentences. Each goes to Piper and then
the player. So audio starts at roughly first-sentence, not first-token — TTFT
~3s plus one sentence.

**9. Barge-in.** `_barge_monitor` (`jarvis/main.py:146`) runs on its own VAD
instance in a thread. If you speak, it sets an event, stops the player, and
`speak_sentences` calls `brain.cancel()` → `POST /task/{id}/cancel` → the
dispatcher kills the subprocess. Note `speak_sentences` **races** the queue-get
against the barge event (`asyncio.wait(FIRST_COMPLETED)`) — otherwise a barge
that fires while parked waiting for the next sentence would be ignored until the
stream produced one.

> **Known limitation, not a bug:** without echo cancellation the mic hears
> Jarvis through your speakers and can barge itself. Use headphones or
> `pactl load-module module-echo-cancel`.

---

## 5. Walkthrough B: the 07:30 daily brief

The unattended path. Different failure modes entirely — nobody is watching.

```
systemd timer ──► run_agent.py ──► brief.is_quiet()? ──yes──► writes the file,
      announces over notify-send, exits (no task, no gate, no model)
                                          │no
                                          ▼
                                  POST /task {source: timer, agent: daily-brief}
                                          │
                        sanitize_untrusted_metadata() strips any tool grants
                                          │
                        AreaRegistry.agent_tools() reads them from DISK instead
                                          │
                                  runner.run_once() ──► claude -p (stream-json)
                                          │
                            each message ─► run_steps row + SSE `step` event
                                          │
                            denial check ─► wrote_declared_output()?
                                          │
                                  notify gate (haiku) ─► NOTIFY: / SKIP:
```

**The deterministic pre-check.** `dispatcher/brief.py` — if `vault/tasks/` has
no `status: open` file and no note in `vault/notes/` was touched in 3 days,
`run_agent.py` writes the brief itself and never submits a task. This exists
because the agent spent ~$0.40 and 8–11 turns every single day from 07-24 to
08-01 writing the words "clean slate". It **fails toward doing the work**: an
unreadable task file counts as material. Open-ness reads through the shared
`task_status` module, so the brief, the checkbox and the task list can never
disagree about what "open" means. The quiet path announces over notify-send
(`announce_quiet`, best-effort, still zero model cost) because submitting no
task means the notify gate never sees it — that silence lasted a month
before anyone noticed the morning ping was gone.

**The trust-boundary dance.** This is the subtlest thing in the repo and it cost
three silent days of missing briefs. `run_agent.py` is an *external HTTP
client*, so `sanitize_untrusted_metadata` (`service.py:94`) strips
`allowed_tools` off its request — correctly, that's H1. But the brief needs
`Edit(vault/briefs/**)`, which the `tasks` area itself doesn't grant. Resolution:
the client sends `metadata.agent = "daily-brief"` — a *reference*, not a grant —
and the dispatcher reads `areas/tasks/agents/daily-brief.md`'s frontmatter from
disk itself (`areas.py:141`). Repo content is the same trust level as SKILL.md;
caller-supplied JSON is not.

**Denial detection.** The headless CLI reports a missing `--allowedTools` grant
as an ordinary `is_error` tool_result and lets the model continue — so a run
that was denied every single write still ends `subtype: success`. `runner.py`
flags denials, then applies a two-stage test:
1. Could this denied tool have written the file? (`denial_could_block_output`
   — only Write/Edit/MultiEdit/NotebookEdit/Bash count; an unparseable denial
   stays fatal.)
2. Did the file the prompt names get written *during this run*
   (`wrote_declared_output`, mtime vs pre-spawn timestamp, not mere existence)?

Both true ⇒ `failed`. Otherwise ⇒ `done` with the denial recorded in
`denied_tools`. Stage 1 was added after a harmless Gmail denial promoted two
legitimately-no-edit-needed runs to `failed`.

**The notify gate.** `notify.surfacing()` is a pure policy function: this is a
`timer` source that finished `done`, so → `"gate"`. A haiku task gets the
request and result inlined (self-contained, no `--resume` — resuming a long
agentic transcript for a one-line verdict measured $0.141 vs $0.103 fresh) and
replies `NOTIFY: <sentence>` or `SKIP: <reason>`. Fail-open: any breakage
notifies with a generic summary, because you asked to be told.

---

## 6. Module map

### `dispatcher/` — the brain (~6,850 lines)

| File | Owns |
|---|---|
| `main.py` | FastAPI app wiring: lifespan tasks, CSRF + cache middleware, static SPA mount, and one `register_*_routes()` call per group (vault, memory-read/write, automations, screen, media, desktop, learning, task, observability) |
| `service.py` | **The core.** Task lifecycle, both dispatch paths, diverts, trust boundary, notify gate, reflection trigger, cancel/shutdown |
| `runner.py` | `claude -p` agentic subprocess: command build, stream-json parsing → timeline steps, refusal/denial detection, env sanitizing. Never raises (except cancel) |
| `quick.py` | `claude -p` streaming subprocess for quick answers. One backend, 162 lines |
| `db.py` | All SQLite. Schema, migrations, tasks/runs/episodes/entries/facts/automations, FTS5 + sqlite-vec hybrid search with RRF fusion, embedding identity |
| `config.py` | `config.yaml` → dataclass. Paths resolve relative to the file; unknown keys warn |
| `areas.py` | Area registry, subsequence trigger matching, **tool-grant sanitizing** (positive scope test). Frontmatter itself parses via `queue_watcher.parse_task_file` — one reader for the dispatcher |
| `task_status.py` | One status reader for task files (brief, toggle, list agree) |
| `classifier.py` | quick-vs-agentic heuristic |
| `memory.py` | Core blocks rendering + budget, episode capture policy, consolidation export builder, export retention |
| `ingest.py` | Vault → heading-ancestry chunks → hash-diff index. md/txt/html/pdf/epub/images; honest unsupported/unparseable accounting |
| `embeddings.py` | fastembed bge-small-en-v1.5 int8, lazy singleton, serialized backfill with model-identity guard |
| `graph.py` | Knowledge-graph extraction + reconciliation prompts, deterministic apply; clip reports surviving ids |
| `cdp.py` | Minimal DevTools-protocol client over stdlib sockets (handshake, frames, id-matched calls) |
| `browser.py` | Tab list/switch/read over DevTools HTTP + `cdp.py` |
| `windows.py` | Window list/focus over AT-SPI via jeepney, zero new deps |
| `automations.py` | NL→schedule parse, mechanical validation, DST-correct next-run math, in-process scheduler loop |
| `notify.py` | Notify-or-not gate prompt (nonce-fenced) + surfacing policy + `notify-send` |
| `offline.py` | Degraded extractive answers from the local index when rate-limited |
| `spotify.py` | Deterministic music parser + MPRIS over jeepney + Web API search |
| `desktop.py` | Deterministic desktop/window/tab verbs + allow/confirm/deny safety plane |
| `inbox.py` | Two-phase-settle file watcher on `vault/inbox/`, honest arrival summaries |
| `brief.py` | Quiet-day pre-check + desktop ping for the daily brief |
| `reflection.py` | Post-task review prompt + trigger policy |
| `curator.py` | Deterministic skill lifecycle: active → stale → archived |
| `limits.py` | Parse provider usage-limit errors, model-scope vs session-scope |
| `telemetry.py` | TTFT / ITL percentiles / throughput from delta timestamps |
| `queue_watcher.py` | `queue/*.md` → tasks, with transient-vs-terminal retry; owns `parse_task_file` |
| `vault.py` | Mechanical vault file I/O for the dashboard (frontmatter-aware toggle) |
| `stt.py` | Server-side Whisper for browser audio uploads |
| `events.py` / `hooks.py` | In-process pub/sub with lifecycle protection (steps evictable, lifecycle never dropped); SSE broadcaster is registered as the first hook |

### `jarvis/` — the voice pipeline (~1,880 lines)

`main.py` is the loop; `plugins/base.py` the four ABCs; `engines/` the
implementations (openWakeWord, faster-whisper, Piper, and the dispatcher-backed
`Brain`); `audio.py` mic/player; `vad.py` Silero; `sanitize.py` markdown/URL
stripping + sentence chunking for TTS; `hotkey.py` raw evdev; `ask_screen.py`
the portal screenshot flow; `dictate.py` the standalone F9 dictation service.

### `ui/` — React SPA

Vite + React 18, **two dependencies**. One SSE subscription in `App.jsx` fans
`lastEvent` out to every widget (one synchronous commit per event — React 18
batching used to eat same-tick pairs). Widgets: AgentMonitor (with system-task
filter + non-terminal polling), Tasks, Brief (quiet-day polling), CommandBox
(with browser mic), Volume, Windows (read-only list), Tabs (read-only list),
NowPlaying, Automations, Stats, ConfirmBar
(sticky, retires on confirm_resolved), Notices. `ui/dist/` is
**committed on purpose** — the dispatcher serves the built bundle, so a fresh
clone needs it.

---

## 7. The memory system — four layers

This is the part that makes it a "life OS" rather than a voice frontend. Four
independent layers, each with a different lifetime and a different cost.

```
  ┌──────────────────────────────────────────────────────────┐
  │ L1  CORE BLOCKS   vault/memory/{USER,MEMORY}.md          │
  │     always in every prompt · 2000/3200 char budget       │
  │     written by: you, by hand · the nightly consolidator  │
  └──────────────────────────────────────────────────────────┘
  ┌──────────────────────────────────────────────────────────┐
  │ L2  EPISODES      SQLite `episodes` · 65 rows            │
  │     one row per settled task · LLM-free capture          │
  │     consumed nightly, then marked consolidated            │
  └──────────────────────────────────────────────────────────┘
  ┌──────────────────────────────────────────────────────────┐
  │ L3  VAULT INDEX   `entries` + FTS5 + vec0 · 85 chunks    │
  │     heading-ancestry chunks, hash-diff reindex           │
  │     hybrid BM25 + 384-dim KNN, RRF-fused                  │
  └──────────────────────────────────────────────────────────┘
  ┌──────────────────────────────────────────────────────────┐
  │ L4  KNOWLEDGE GRAPH  kg_facts / kg_entities · bi-temporal│
  │     nightly extraction · weekly reconcile · never deletes│
  └──────────────────────────────────────────────────────────┘
```

**L1 — core blocks** (letta's design). Two markdown files injected into *every*
quick and agentic prompt. Budgets are enforced at read time, so a hand-edited
file can't blow up every prompt. Only `USER.md` and `MEMORY.md` are injected —
`memory.py:32` has an explicit allowlist, because an OCR artifact landing in
`blocks_dir` must not silently enter every system prompt.

**L2 — episodes.** Every settled task appends a row. Capture is *free* — no LLM,
just an INSERT. `memory.should_capture` (`memory.py:80`) is the policy, and it's
worth reading: it excludes the consolidator (its own run would become tomorrow's
input), reflections, gate/parse tasks, diverts, **and** `daily-brief` /
`weekly-review`.

That last exclusion is the best cautionary tale in this repo. Their episode says
"I wrote a file", so the nightly graph extractor read it back as *knowledge* —
and every single fact in the graph became a self-observation like "the
daily-brief automation continued writing successfully through 07-31", each night
invalidating the previous night's copy of itself. Six facts, four already
superseded, zero about you. **The memory system was eating its own housekeeping.**
Nothing was lost by excluding them: a brief's *content* reaches memory the right
way, through the vault index that ingests `vault/briefs/` anyway.

**L3 — vault index.** `ingest.py` walks `index_dirs`, chunks by heading ancestry
(so every hit is self-describing: `notes/foo.md / Setup / Networking`), extracts
ISO dates, and diffs per-chunk MD5 hashes against what's indexed. mtime
prefilter first, so a no-op reindex is nearly free. Search fuses FTS5 BM25 with
sqlite-vec KNN using reciprocal-rank fusion (k=60). Deterministic filters
(`file`, `after`, `before`) force the FTS-only path — filters before vectors.

The proof this layer works: "preferred coding tool" has **zero** FTS hits and
still surfaces the USER.md Neovim block, via vectors alone.

**L4 — knowledge graph.** Facts are standalone sentences linked n-ary to
entities (not strict subject→object edges — simpler, and 1-hop expansion is one
join). Bi-temporal: `valid_at` = true in the world since; `invalid_at` =
contradicted as of; `expired_at` = when we learned that. **Invalidation never
deletes**, so history stays queryable.

The safety mechanism here is worth noting: `graph.candidate_ids` captures the
exact set of fact ids shown to the model, and `_allowed_invalidations`
intersects the reply against it. A reply can never reach past the candidates it
was shown — no graph wipe from one bad generation. It also rejects bools,
because `isinstance(True, int)` is `True` and a bare `[true]` would otherwise
mean "invalidate fact 1".

**Degraded mode** (`offline.py`) ties L3 and L4 back to the hot path: when the
plan cap kills a quick question, the answer comes **extractively** from the
local index — verbatim quotes with the source file named, no generation.

The load-bearing detail is the *relevance anchor*. KNN always returns something.
Asked about "the airspeed velocity of a laden swallow", the index cheerfully
handed back its three least-unrelated chunks, which then got quoted as "here's
what I already have on it". So a degraded answer is only offered when **BM25
matched a real term first**. Ranking still uses the hybrid path, so vectors keep
floating the right chunk up — they just can't conjure a topic from nothing.

---

## 8. The security model

Three mechanisms, each mechanical rather than conventional.

### H1 — the metadata trust boundary

`sanitize_untrusted_metadata` (`service.py:94`). Metadata from an external
source (api/queue/voice/ui/screen) can **narrow** tools/budget but never widen
them:

- `allowed_tools` and `resume_session_id` are **dropped**;
- `max_cost_usd` is **clamped** to the config cap (not dropped — narrowing is
  legitimate);
- a non-numeric budget is dropped so the default applies.

Only server-internal spawns pass `trusted=True`: reflection, notify gate,
consolidation, graph, learn, limit-requeue, diverts.

Read the invariant precisely: it bars *caller-supplied* grants. A caller may
still **select among operator-authored on-disk manifests** via
`metadata.task_type` (→ `config.yaml`) or `metadata.agent` (→
`areas/<area>/agents/<agent>.md`), and those can be broader than the read-only
default. That's deliberate — it's how the timer agents get their grants at all —
and safe because the manifests are repo content, with the agent path slug-guarded
at load.

### H2 — area privilege sanitizing

`areas.py:34` `_sanitize_tools`. At load time, `Bash` (any form) and **unscoped**
`Edit`/`Write` are stripped from any area's frontmatter unless the area is listed
in `security.privileged_areas` (empty by default). `Edit(areas/**)` is scoped and
fine; a bare `Edit` grants repo-wide writes.

Why this matters: a reflection run holds `Edit(areas/**)`. If it were
prompt-injected into writing `- Bash` into a SKILL.md, load-time validation makes
that grant inert.

There was a real hole here, closed 2026-08-02: `memory.build_consolidation` was
parsing `areas/memory/agents/consolidate.md` itself instead of going through
`AreaRegistry` — and that task submits `trusted=True`, so an unsanitized
`allowed_tools:` would have survived intact. A reflection writing `- Bash` into
that file would have handed the nightly consolidation a shell. Grants now come
from `AreaRegistry` on both paths; the local frontmatter parse is only for the
prompt body.

### The desktop safety plane

`desktop.policy` (`desktop.py:121`) — allow / confirm / deny per verb, **failing
closed**: an unknown verb denies, an unrecognised policy value denies, and an
absent `computer:` block denies everything (which is also what keeps the unit
tests side-effect-free).

Defaults are deliberately *not* "every write verb confirms" — these verbs are
reversible and low-stakes, and confirming "system volume 40" by voice every time
would be worse than not having the feature. Two verbs do confirm:
- `clipboard_get` — the clipboard routinely holds passwords, and it's the one
  verb that reads user data into a model's context;
- `open` — an arbitrary URL is both an exfiltration channel and the obvious
  prompt-injection payload.

`open` additionally **hard-restricts schemes at the executor** (http/https/file),
so it holds even if you set `open: allow`. `desktop.scheme_of` is hand-rolled
because `urlparse` is actively wrong here: the dangerous schemes are the *opaque*
ones with no `//`, so normalizing a bare host by prepending `//` makes urlparse
read `javascript:alert(1)` as a host with an empty scheme — i.e. it silently
passes the exact input the guard exists to stop. A unit test pins that.

### Three smaller ones

- **`cli_env`** (`runner.py:131`) strips `ANTHROPIC_API_KEY` /
  `ANTHROPIC_AUTH_TOKEN` from every `claude` subprocess. The CLI *prefers* an API
  key over the claude.ai login when one is present, so a stray key **replaces**
  your subscription — an unfunded one breaks every run, a funded one silently
  bills each ~18k-token cold start to API credit.
- **`--disallowedTools Bash`** whenever Bash wasn't granted. The headless CLI
  auto-permits a sandboxed read-only Bash even when it's absent from
  `--allowedTools`. Granting exactly these tools should mean exactly these.
- **The origin guard** (`main.py:62`) is a **CSRF check, not auth**. It 403s a
  state-changing request carrying a *non-local* Origin. `security.public_hosts`
  adds your Tailscale name. Tailscale itself is the access control — the
  dispatcher never leaves `127.0.0.1`.

---

## 9. The cost model — why so much code avoids the model

This is the economic fact everything else bends around:

> **Every cold `claude -p` re-sends the ~16–18k-token Claude Code system prompt.**
> That's ~$0.10–0.14 of notional cost and ~3s of TTFT before your question is
> even considered.

Consequences that shaped the architecture:

1. **`--resume` is not "nearly free".** The original $0.019 measurement never
   reproduced; two real reflections averaged $0.108. A resume buys conversational
   context, not a cheap run.
2. **Resuming can be *worse*.** The notify gate resumed the settled run's session
   on the theory that a warm cache helped. Measured: $0.141 resumed vs $0.103
   fresh — replaying a long agentic transcript to get a one-line verdict costs
   more than it saves. Removed.
3. **The model *rate* dominates**, since the token count is a floor you can't
   move. Hence `models.meta` → haiku for pure classification. $0.0405 vs $0.189
   live.
4. **`graph-extract` is deliberately excluded** from meta routing — its output is
   fact text that lands in the knowledge graph, so quality outranks $0.14.
5. **Ring 1 exists.** Every divert is a run that costs literally nothing.

Costs logged are **notional** — you're on a subscription, not metered billing.
But the **quota is real**, and the plan cap has taken the assistant down twice.
That's the actual currency being conserved.

The Messages API would have avoided the floor entirely (measured **$0.00051 vs
$0.14052** a question, 275×) but was removed 2026-07-27 at your direction. It's
recoverable at commit `ce93ea0`. `models.meta` is the lever that remains.

---

## 10. Data model

### SQLite (`data/mission.db`)

```
tasks ────┬─── runs ────── run_steps        one task, N attempts, M timeline steps
          └─── episodes                     memory capture

vault_files ── entries ──┬── entries_fts    BM25
                         ├── entries_vec    384-dim KNN
                         └── entry_dates    date filters

episodes ────────────────┬── episodes_fts
                         └── episodes_vec

kg_facts ── kg_fact_entities ── kg_entities
    └────── kg_facts_fts

automations        standing schedules
skill_usage        area telemetry for the curator
```

The `tasks`/`runs` split is the important one. **One task = one request; one run
= one model attempt.** A refusal is run 1 `refused` + run 2 on the fallback. A
vanished-session retry is two runs. This is why `/stats` uses
`COUNT(DISTINCT t.id)` — a plain `COUNT(*)` over the LEFT JOIN silently meant
"runs" and inflated per-source numbers (49 → 46 on the real db).

Schema evolution is a `SCHEMA` executescript (all `IF NOT EXISTS`) plus an
idempotent `MIGRATIONS` list of `ALTER TABLE ADD COLUMN` statements that swallow
"duplicate column". Vec tables are created only when sqlite-vec loads;
everything degrades to FTS-only silently without it.

`PRAGMA busy_timeout=15s` matters: WAL still serializes writers, and without it
a contended settle-path write could strand a task at `running`.

### The vault (`vault/`)

```
vault/
  memory/          USER.md, MEMORY.md (injected), projects/ (searchable)
  tasks/           one markdown file per task, YAML frontmatter, status: open|done
  notes/           free-form
  briefs/          YYYY-MM-DD.md daily, week-YYYY-MM-DD.md weekly
  inbox/           drop zone — watched, indexed, announced
```

Plain markdown throughout, on purpose: you can edit any of it in Neovim, and
everything downstream (index, brief, consolidation) re-reads from disk.

---

## 11. Operations — what runs when

```
02:30 nightly   mission-memory-consolidate   episodes → USER.md/MEMORY.md
                                             + graph-extract in parallel
03:30 nightly   mission-backup               SQLite backup, 14-day prune
04:00 monthly   mission-skill-curator        1st of month, area lifecycle
04:30 weekly    mission-memory-reconcile     Sun, graph dedup/contradictions
07:30 daily     mission-daily-brief          quiet-day pre-check first
18:00 weekly    mission-weekly-review        Sun

always-on:      mission-dispatcher           :8765, serves the SPA
                mission-jarvis               --mode both (wake + PTT)
                mission-dictate              F9 → ydotool at cursor
                mission-tailscale-serve      oneshot, HTTPS front door
```

Plus **four in-process loops** inside the dispatcher, started in the FastAPI
lifespan:
- queue watcher (5s) — `queue/*.md` → tasks;
- automations scheduler (30s) — due rows → tasks;
- inbox watcher (60s) — new files → reindex + notify;
- embedding refresh (15min).

The automations scheduler is a **deliberate deviation** from the plan's systemd
timer: the dispatcher must be up for tasks anyway, and an in-process loop gets
catch-up for free via past-due `next_run_at`.

**Two boot-race lessons are encoded in the units.** `Persistent=true` timers fire
at boot before the dispatcher has bound its port — hence `run_agent.py`'s 12×5s
connection retry loop (which distinguishes connection failures, retried, from
HTTP error responses, which are not: the server got the request). And
`After=tailscaled.service` orders against the unit *starting*, not the daemon
being *ready* — hence the `ExecStartPre` readiness wait on
`mission-tailscale-serve`, after that oneshot exited 1 at boot and stayed dead
for a day.

A `Persistent=true` catch-up can also write *tomorrow's* brief: the box was off
at 07:30 on 07-29, the timer caught up at 00:42 on the 30th, and the agent's
`{{DATE}}` was already 07-30. That's why brief dates can skip a day.

---

## 12. The failure-mode catalogue

The most valuable thing in this repo isn't the features — it's the list of ways
it broke and what each one taught. Almost every bug was in a **seam between two
subsystems that were each individually correct**.

| Symptom | Root cause | Lesson |
|---|---|---|
| 3 daily briefs wrote nothing, all reported `done` | The H1 trust boundary stripped the grant the timer sent | Security fixes need a path for legitimate privilege |
| A denied-every-write run reported success | CLI surfaces missing grants as ordinary tool_results | Never trust an exit code over an artifact check |
| A harmless Gmail denial marked two good runs `failed` | mtime test can't distinguish "blocked" from "no edit needed" | Failure detection needs a *capability* test too |
| "every morning play jazz" hit Claude every day | Diverts lived in the HTTP handler; `automations.fire` calls `submit()` directly | Put policy on the service, not the transport |
| Deleting an inbox file announced "Indexed 0 files" | Notify branch guarded on `arrived or removed`, formatted `arrived` | — |
| `copy Hello World` stored `hello world`; a YouTube URL became a different video | Argument extracted from the case-folded string | **Matching folds case; arguments must not** |
| Every graph fact was a self-observation | `should_capture` missed daily-brief/weekly-review | A memory system will eat its own exhaust if you let it |
| `open firefox` never worked, always said "Opening firefox." | (1) no DISPLAY in the service env (2) `capture_output` hung on a *successful* launch (3) the app died with the dispatcher | Three bugs in series, each hidden by the next |
| A "running" firefox check passed for the wrong reason | Matched the whole command line, found a terminal that merely *mentioned* firefox | Match argv[0] only |
| `javascript:` passed the URL scheme guard | `urlparse` mis-reads opaque schemes when you prepend `//` | Hand-roll when the stdlib's model doesn't fit |
| `_status` never once reported lock state | A systemd **user** service isn't in a login session | Read the session bus, and return None for "couldn't tell" |
| A dispatcher crash was filed `cancelled` | Broad `finally` treated our bug as a user barge-in | Don't let your own failures hide in the user's bucket |
| A CSS fix looked unfixed in headless chromium | Chromium cached `index.html`; the hashed asset was new | `Network.setCacheDisabled` + cache-bust when re-measuring |
| `ui/dist` gitignored *and* tracked | Git keeps tracking existing files, but Vite emits **hashed** names | A new bundle is a new path the ignore rule hides |

---

## 13. How to extend it

**Add a life area** — make `areas/<name>/SKILL.md` with frontmatter (`name`,
`description`, `triggers`, optional `quick_triggers`, `allowed_tools`,
`quick_allowed_tools`) and a markdown body. That's it; no core changes, no
restart. Triggers match as **in-order word subsequences**, so `mark task done`
matches "mark the domain task as done" — spoken phrasing never matches exact
substrings reliably. Remember `Edit(path/**)` is what actually grants Write; a
bare `Write(path/**)` rule is silently ignored by this CLI.

**Add a deterministic verb** — write a pure `detect()` returning an Intent or
None, a never-raising `run_intent()` that turns every failure into a speakable
sentence, and hook it into `Service.try_divert()`. Mirror `desktop.py`. Note
these are deliberately **not** areas: an area would need Bash to reach D-Bus,
which `security.privileged_areas` exists to prevent.

**Add a timer job** — write `areas/<area>/agents/<name>.md` with `task_type` and
`allowed_tools` frontmatter, then a `.service` calling `run_agent.py <area>
<name>` plus a `.timer`. `systemd/install.sh` installs them.

**Add a dashboard widget** — a component in `ui/src/widgets/`, registered in
`index.js`. It receives `lastEvent` and `eventLog` from the single app-level SSE
subscription. Rebuild with `npm run build` and **commit `ui/dist/`**.

**Add an ingest format** — a chunker `(label, path_or_text) → [chunk dicts]`
registered in `ingest.CHUNKERS`. Raise `DepMissing` for an absent optional
dependency and it degrades to `dep_gated` counting instead of breaking the walk.

---

## 14. Where the bodies are buried

Honest notes on the current state.

**The learning loop fires** (since 2026-08-11). The trigger was `num_turns >=
12`, which normal operation never reached — two reflections ever, last on 07-19.
It is now **8**, with `learning.no_reflect_extra: [daily-brief, weekly-review]`
layered on top of the hard-coded `NO_REFLECT_TASK_TYPES` so the timer agents
don't spawn a review of "I wrote today's brief" every morning.

**Orphaned FTS rows in the knowledge graph.** `kg_facts` has 0 rows but
`kg_facts_fts` has 2. The session-25 purge deleted facts with manual SQL, and
there is no `delete_fact()` method that would have swept the FTS mirror —
`invalidate_facts` never deletes, by design. Harmless (a search would join to a
missing fact and return nothing), but it's a real inconsistency, and the missing
method is why.

**`jarvis/ptt_dictate.py` is referenced by nothing.** It's your original script,
saved verbatim 2026-07-05, kept as the reference for evdev hotkey capture.

**Duplication that's been flagged and deferred**: frontmatter parsing appears 5×;
`_normalize` / `VETO` / `parse_response` are duplicated between `spotify.py` and
`desktop.py`; `create_app` is 452 lines; `_conn()` reconnects per query.

**`context/STATE.md` was 79KB** and the session protocol reads it every session.
Trimmed to ~23KB on 2026-08-11; sessions 3–25 rolled verbatim into
`context/state-archive.md`, and protocol rule 4 now caps it at roughly the last
five sessions.

**Unbounded quick-path concurrency.** `max_concurrent_agentic: 2` gates agentic
runs via a semaphore; quick tasks have no such limit.

**Never tested: a reboot.** The systemd units are installed and verified
running, but a cold-boot test has never actually been run.

---

## 15. Reading order, if you want to go deeper

1. `dispatcher/service.py` — read `submit`, `try_divert`, `stream_quick`,
   `_run_agentic_inner` in that order. This is the system.
2. `dispatcher/runner.py` — the `_attempt` function and the denial logic below it.
3. `dispatcher/memory.py:80` `should_capture` — 30 lines of comments explaining
   an entire class of bug.
4. `dispatcher/db.py` — the `SCHEMA` string top to bottom is a good data-model tour.
5. `jarvis/main.py` — `speak_sentences` and `_barge_monitor`, for the concurrency.
6. `dispatcher/desktop.py:226` `detect` — the cleanest example of the
   deterministic-parser pattern the codebase uses three times.

The comments in this codebase are unusually dense and unusually *causal* —
almost every one explains a decision or a scar rather than restating the code.
Trust them; they were mostly written immediately after the bug they describe.
