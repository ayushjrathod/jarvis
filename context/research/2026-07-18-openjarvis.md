# OpenJarvis reference audit — for Mission Control v2 planning

_Research subagent report, 2026-07-18 (v2 planning). Clone (gitignored) at
`references/openjarvis` (Stanford Hazy Research / Scaling Intelligence Lab,
"Intelligence Per Watt" project)._

**License**: Apache 2.0 (`LICENSE`) — code liftable with attribution comments, same as our OpenClaw lifts.
**Scale**: 664 Python files under `src/openjarvis/`, plus a Rust extension, React frontend, and a large research/evals harness. Much of it is research infrastructure irrelevant to us; the liftable core is small.

---

## 1. The five composable primitives

Canonical description: `docs/architecture/overview.md` (also `docs/index.md`).

| # | Primitive | ABC / interface | Where |
|---|---|---|---|
| 1 | **Intelligence** | Model catalog + `IntelligenceConfig` (weights path, quant, preferred engine, fallback chain, gen defaults) | `src/openjarvis/intelligence/model_catalog.py` |
| 2 | **Engine** | `InferenceEngine` ABC: `generate()`, `stream()`, `stream_full()`, `list_models()`, `health()`, `can_serve()`, `prepare()`, `close()` | `src/openjarvis/engine/_stubs.py`, `_base.py` |
| 3 | **Agentic Logic** | `BaseAgent` ABC (`run()`) + `ToolUsingAgent`; 9 agent types incl. a `ClaudeCodeAgent` that shells out to the Claude Agent SDK via Node | `src/openjarvis/agents/_stubs.py` |
| 4 | **Memory** | `MemoryBackend` ABC; SQLite/FTS5 default, FAISS/ColBERT/BM25/hybrid | `src/openjarvis/memory/` |
| 5 | **Learning** | Cross-cutting: `RouterPolicy` + `QueryAnalyzer` ABCs, `TraceStore`/`TraceCollector`/`TraceAnalyzer`, reward functions | `src/openjarvis/learning/`, `src/openjarvis/traces/` |

**How they compose** — two mechanisms:

1. **Decorator registries** (`src/openjarvis/core/registry.py`): generic `RegistryBase[T]` with typed subclasses. `@EngineRegistry.register("ollama")` makes a backend discoverable with zero factory edits.
2. **A thread-safe synchronous EventBus** (`src/openjarvis/core/events.py`) as connective tissue: engines/agents/tools publish `INFERENCE_START/END`, `TOOL_CALL_START/END`, `TELEMETRY_RECORD`, `TRACE_STEP/COMPLETE`; the `TelemetryStore` and `TraceStore` are just subscribers. Observability is decoupled from execution.

**Comparison to ours**: our four voice ABCs are the identical discipline applied at the edge. What OpenJarvis splits and we collapse: our dispatcher *is* their Engine + Agent + Router + Server in one service. Their Intelligence/Engine split exists because they juggle dozens of local models across 10 runtimes; we have one brain (Claude) on two transports. The registries are over-engineering at our scale; the **event-spine idea** (execution publishes, observers subscribe) is the part with transferable value — and our SSE `/events` stream already plays 80% of that role.

---

## 2. Multi-model orchestration & parallel execution

Two distinct layers:

**a) Core routing (single query → one model).** `RouterPolicy.select_model(RoutingContext)` where `build_routing_context()` regex-detects code/math/length/urgency (`src/openjarvis/learning/router.py`, `learning/routing/complexity.py`). The `HeuristicRouter` policy, in priority order: urgency>0.8 → smallest model; code → "coder" model; math or long/reasoning query → largest; short (<50 chars) → smallest; else default→fallback→first. `complexity.py` additionally scores 0–1 and maps to **token-budget tiers** (trivial 1024 → very_complex 16384, ×2 for thinking models). This is a structured, graded version of our binary quick-vs-agentic `dispatcher/classifier.py`.

**b) Hybrid local+cloud paradigms** (`src/openjarvis/agents/hybrid/`, README at `agents/hybrid/README.md`). Six ported research protocols: **Minions** (cloud supervisor + local bulk-reading workers, ~10× cheaper at cloud accuracy), **SkillOrchestra** (per-query competence-minus-λ·cost routing), **Conductor** (planner emits static DAG up to 5 steps, parse-failure fallback ladder), **Archon** (K local proposers → cloud ranker → fuser), **Advisors** (cloud answers → local critique → cloud re-answer), **ToolOrchestra** (RL-trained 8B orchestrator).

**How "parallel" actually works**: plain **threads, not asyncio** — `ThreadPoolExecutor` in `agents/hybrid/runner.py` (line 851) fanning blocking SDK calls; the core SDK query path is sequential. Nothing architecturally exotic to lift. Also note `_base.py`'s candid admission: per-paradigm SDK quirks are "paradigm-shaped" — the uniform engine abstraction breaks down exactly where orchestration gets interesting.

**Relevance to us**: all six paradigms presume a capable local LLM worker (vLLM, GPU). Our box is CPU-only and our Claude cost is subscription-flat, which deletes both the feasibility and the economic rationale. The one transplantable idea is *graded* routing with a complexity tier + suggested budget (pattern 6 below).

---

## 3. Cost / latency / energy telemetry

**Measurement path**: `instrumented_generate()` (`src/openjarvis/telemetry/wrapper.py`) wraps every engine call — timestamps, pulls `usage`, publishes a `TelemetryRecord` on the bus. A `TelemetrySession` (`telemetry/session.py`) runs a daemon thread sampling an `EnergyMonitor` every 100ms into a ring buffer; per-call energy = trapezoidal integration over the call's window. Vendor monitors: NVML, Apple, AMD, and **`energy_rapl.py` — Linux sysfs powercap CPU energy counters** (the only one applicable to our hardware). FLOPs (`telemetry/flops.py`) are estimated: 2·P·T from a params lookup. ITL percentiles from token-arrival timestamps (`telemetry/itl.py`, ~50 self-contained lines).

**Schema** (`telemetry/store.py`, single `telemetry` table, ~40 columns): timestamp, model_id, engine, agent, token counts, `latency_seconds`, **`ttft`**, `cost_usd`, energy joules split, power, GPU stats, `throughput_tok_per_sec`, prefill/decode latency split, derived efficiency, **ITL mean/median/p90/p95/p99/std**, `is_streaming`, `is_warmup`, `batch_id`, `token_counting_version` (methodology versioning — nullable so legacy rows are excludable), metadata JSON. Migration is an idempotent additive `ALTER TABLE` loop (`_MIGRATE_COLUMNS`) — a tidy pattern for evolving our `runs` table.

**Surfacing**: `telemetry/aggregator.py` → FastAPI `/v1/telemetry/stats`, `/v1/telemetry/energy` (`server/api_routes.py:409`) → React `EnergyDashboard.tsx`, `SavingsDashboard.tsx`, and the standout **`frontend/src/components/Chat/XRayFooter.tsx`**: every chat message carries a collapsed one-line footer "engine — model — complexity tier — 1.2s — 812 in / 96 out tokens", expandable to detail rows.

**What they'd add over our `runs` table**: TTFT, streaming throughput, ITL percentiles, per-model aggregate rollups behind an endpoint, methodology-version column, and — the only true novelty for our hardware — RAPL CPU energy.

---

## 4. Learning loop from local traces

**Data**: `traces/store.py` — `traces` table (query, agent, model, engine, result, **outcome**, **feedback** score, timing, tokens, full `messages` JSON) + `trace_steps` table (step_index, step_type ROUTE/RETRIEVE/GENERATE/TOOL_CALL/RESPOND, timestamp, duration, input/output JSON) + an FTS5 mirror. `TraceCollector` builds traces purely by subscribing to bus events. `TraceAnalyzer` computes `summary()`, `per_route_stats()` (per model+agent: count, avg latency, success rate, avg feedback), `per_tool_stats()`.

**Loop**: `LearnedRouterPolicy.update_from_traces()` (`learning/routing/learned_router.py`) groups traces by query class, scores each model per class (60% success rate + 40% user feedback), installs the winner gated by `min_samples=5`. Reward shaping in `learning/routing/heuristic_reward.py` (0.4 latency + 0.3 cost + 0.3 token efficiency).

**Beyond routing** (`docs/architecture/learning.md`): `learning/optimize/` — LLM-guided config search with Pareto frontier; `learning/spec_search/` — a **frontier "teacher" model reads local traces, diagnoses 2–5 failure clusters, emits a typed LearningPlan of config/prompt/tool edits, each gated by benchmark scoring with git-backed rollback and a deterministic risk-tier table (auto / review / manual)**; `learning/agents/` + `learning/intelligence/` — DSPy/GEPA/ACE prompt optimizers and SFT/GRPO trainers.

**Verdict for us**: trace-driven *model* routing is research-only here — we route between exactly two Claude models where the fallback choice is policy (refusal/usage-limit), not quality. What *is* liftable: (a) the outcome/success-rate analytics shape, (b) the `trace_steps` timeline schema, (c) the *spec-search governance idea* — a periodic Claude agent that reads our own `runs` failures and proposes reviewed config edits — mapping naturally onto our weekly-review agent pattern.

---

## 5. InferenceEngine abstraction vs our quick-backend

Their contract (`engine/_stubs.py`): `engine_id` + `is_cloud` class attrs; `health()` probe; `can_serve(model)` veto; `stream()` vs `stream_full()` (rich `StreamChunk`, aggregates emitted once at end-of-stream). `MultiEngine` (`engine/multi.py`) routes by model name with one deliberate decision worth quoting: local models **never** silently fall back to cloud; only cloud-prefixed names do.

Our `dispatcher/quick.py` already converged on the same shape independently: two backends behind one async-generator yielding `("delta", str)` then exactly one `("meta", dict)` — that *is* `stream_full()` with a terminal aggregate chunk. Formalizing a `QuickBackend` ABC for two backends adds ceremony, not capability. The genuinely liftable pattern is the **typed-error discipline** in `engine/_base.py`: a single shared marker-substring classifier (`CONTEXT_LENGTH_MARKERS` + `looks_like_context_length_error()`) feeding an exception subclass (`EngineContextLengthError`) with a marker attribute. Our `dispatcher/limits.py` usage-limit classification is the same idea grown ad hoc — worth consolidating to their one-function-one-place discipline as we add error classes.

---

## 6. Ranked liftable patterns

| # | Pattern | Source | Ours | Effort | Value |
|---|---|---|---|---|---|
| 1 | Streaming latency metrics: TTFT + ITL percentiles + throughput per run | `telemetry/itl.py`, `telemetry/store.py` columns | `dispatcher/quick.py` (timestamp each delta), `runs` columns via additive migration | **S** | **High** |
| 2 | X-Ray per-interaction footer | `frontend/src/components/Chat/XRayFooter.tsx` | `ui/src/widgets/AgentMonitor.jsx` (rows already expand) | **S** | **High** |
| 3 | `run_steps` timeline table (Layer 12 backbone) | `traces/store.py` `trace_steps` schema, `traces/collector.py` | `dispatcher/runner.py` already parses `claude -p` stream-json events — persist them instead of discarding | **M** | **High** |
| 4 | Success-rate/aggregate analytics + optional feedback signal | `traces/analyzer.py`, `telemetry/aggregator.py`, `/v1/telemetry/stats` route | `dispatcher/db.py` (+`feedback` col), new `/stats` endpoint, dashboard stat tiles | **S/M** | **High** |
| 5 | Typed-error markers: shared classifier fn + exception subclass w/ marker attr | `engine/_base.py:35-53` | `dispatcher/limits.py`, `dispatcher/runner.py` | **S** | Med |
| 6 | Complexity tiers in the classifier (graded, with suggested budget/effort) | `learning/routing/complexity.py` | `dispatcher/classifier.py` — regex tiers → could drive `--effort`/budget per task | **S** | Med |
| 7 | Claude-as-teacher trace review (idea only, no code) | `learning/spec_search/` + risk-tier table (`docs/architecture/learning.md`) | New agent prompt under `areas/` beside weekly-review; reads `runs`, proposes config edits, user-review gated | **M** | Med |
| 8 | RAPL CPU energy per voice interaction | `telemetry/energy_rapl.py`, `telemetry/session.py` | `jarvis/` pipeline (whisper/piper only local compute worth metering); `/sys/.../energy_uj` often needs perms | **M** | Low-Med |
| 9 | `token_counting_version`-style methodology column + `_MIGRATE_COLUMNS` idempotent migration loop | `telemetry/store.py:59-143` | `dispatcher/db.py` — adopt alongside #1/#3 | **XS** | Med (enabler) |

Details on the top three:

1. **TTFT/ITL** — `compute_itl_stats()` is ~50 dependency-free lines. Our quick path already iterates deltas in `stream_quick`; collecting `time.monotonic()` per delta and computing `ttft`, `mean/p95 ITL`, `tok/s` in the existing `finally` is nearly free. Directly instruments the one open performance question we have (CLI backend first-delta 3–5s vs future Messages API path) with data instead of anecdote.
2. **X-Ray footer** — Session 6 already made AgentMonitor rows expandable; the lift is the *collapsed summary line* format (backend — model — tier — latency — tokens — cost) applied to every interaction, matching Layer 12.
3. **run_steps** — the highest-leverage schema idea: our runner consumes stream-json and keeps only aggregates. One table `(run_id, step_index, step_type, ts, duration, payload JSON)` populated from tool_use/text events gives timeline + tool usage + failures for the observability layer, and it's what session 3's deferred "stream agentic runs for dashboard visibility" item needs anyway — the two should be scoped together.

---

## 7. Explicit SKIPs

- **Engine backend zoo** (Ollama/vLLM/SGLang/llama.cpp/MLX/LiteLLM) and **MultiEngine model-name routing** — we run no local LLMs and have one provider.
- **Intelligence primitive / model catalog** — catalogs local weights/VRAM; meaningless for a subscription cloud brain.
- **Heuristic/learned model routing** (`learning/routing/`) — two fixed models whose fallback is policy-driven; insufficient traffic to learn.
- **All six hybrid paradigms** — require a capable local vLLM worker (GPU) and are motivated by per-token cloud cost we don't pay. Their "parallel model execution" is `ThreadPoolExecutor` over blocking SDK calls — nothing to learn mechanically.
- **Registry pattern + EventBus** — elegant but re-architecture for our scale; config.yaml engine swap and SSE `/events` already deliver the outcomes.
- **FLOPs/MFU estimation, GPU energy monitors, savings dashboard, leaderboard, mining/pearl subsystem** — research metrics for local inference economics we don't have.
- **Training-based learning** (SFT/GRPO, DSPy/GEPA/ACE optimizers) — research-only, needs GPUs and eval harnesses.
- **Memory backends** (FAISS/ColBERT/hybrid) — vault + SQLite is locked decision #4.
- **Sandbox/container agent execution** — our `--allowedTools` scoping is the chosen guardrail model.
- **`ClaudeCodeAgent`** (`agents/claude_code.py`) — they wrap the Claude Agent SDK via a Node subprocess; our `dispatcher/runner.py` is a more mature equivalent. Mild validation that our architecture is the same one Stanford reached for.
- **PostHog product analytics** (`analytics/`) — phone-home telemetry, against our local-first constraint.

**Bottom line**: OpenJarvis's headline features (local models, parallel hybrid execution, energy-per-watt research) mostly don't transfer to a subscription-Claude, CPU-only setup — but its observability layer transfers almost wholesale: patterns 1–4 together are a coherent, low-effort "Layer 12" package built on schema shapes we can adopt additively over our existing `runs` table.
