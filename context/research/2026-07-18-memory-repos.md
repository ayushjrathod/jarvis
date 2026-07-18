# Memory-layer reference audit: mem0, graphiti, letta

_Research subagent report, 2026-07-18 (v2 planning). Clones live (gitignored)
at `references/{mem0,graphiti,letta}`._

**Licenses — all three are Apache-2.0** (`references/mem0/LICENSE`, `references/graphiti/LICENSE`, `references/letta/LICENSE`; confirmed in each `pyproject.toml`). Lifting code/prompts with attribution comments is fully compatible with our "lift, never depend" rule.

---

## 1. mem0 — fact extraction + memory-update pipeline

### The prompts (all in one file)
`references/mem0/mem0/configs/prompts.py`:

- **`FACT_RETRIEVAL_PROMPT`** (line 15) — classic v1 extractor: "Personal Information Organizer" persona, 7 categories (preferences, personal details, plans, services, health, professional, misc), few-shot pairs, output `{"facts": ["…"]}`. Injects today's date at import time.
- **`USER_MEMORY_EXTRACTION_PROMPT`** (line 63) / **`AGENT_MEMORY_EXTRACTION_PROMPT`** (line 124) — split variants: extract only user-attributed vs only assistant-attributed facts.
- **`DEFAULT_UPDATE_MEMORY_PROMPT`** (line 176) — the famous **ADD/UPDATE/DELETE/NONE** decision prompt: presents old memories as `{id, text}` with small integer ids, new facts in backticks, few-shot examples for each of the four events; UPDATE keeps the id and returns `old_memory`, DELETE fires on contradiction. Wrapped by **`get_update_memory_messages()`** (line 406) which adds the JSON output contract.
- **`ADDITIVE_EXTRACTION_PROMPT`** (line 468) + **`generate_additive_extraction_prompt()`** (line 1016) — the newer **V3 "ADD-only" pipeline** ported from their platform. Extraction and update are collapsed into ONE call: the prompt receives Summary, Last-k messages, Recently-extracted memories, Existing memories (integer-remapped ids to prevent UUID hallucination), New messages, **Observation Date vs Current Date** (all relative time resolved against observation date — "yesterday" → concrete date), and outputs contextually-rich 15–80-word memories with `linked_memory_ids` pointing at related existing memories. Dedup is prompt-driven ("skip if semantically equivalent") + downstream hash check. This prompt is exceptionally good — 12 worked examples covering multi-topic extraction, incidental facts, document content extraction, temporal grounding, and an "exhaustive extraction checklist."

### The pipeline code
`references/mem0/mem0/memory/main.py`, `_add_to_vector_store()` (line 835):

1. Pull last 10 raw messages for the session from SQLite (`db.get_last_messages`).
2. Vector-search top-10 existing memories; remap UUIDs → "0","1",… (anti-hallucination, line 889).
3. **One LLM call** with the additive prompt → `{"memory": [{id, text, attributed_to, linked_memory_ids}]}`.
4. Batch-embed extracted texts.
5. **Dedup by MD5 hash of the text** against existing payload hashes + within-batch (line 961–980).
6. Batch insert vectors + append `ADD` rows to a SQLite `history` table.
7. Batch **entity linking**: local spaCy extraction (no LLM) → entity store upsert (line 1042+).

The classic UPDATE/DELETE flow still exists via `get_update_memory_messages` + `_update_memory()`/`_delete_memory()` (lines 1972/2038) for the explicit `update()`/`delete()` APIs.

### Storage
`references/mem0/mem0/memory/storage.py` — plain-SQLite `history` table (line 108: `id, memory_id, old_memory, new_memory, event, created_at, updated_at, is_deleted, actor_id, role`) and `messages` table (line 134: `id, session_scope, role, content, name, created_at`). Vector stores are pluggable (30 providers under `mem0/vector_stores/`); the only embedded/no-server ones are **`faiss.py`** and Chroma. No sqlite-vec provider — we'd write our own (trivial given their `base.py` interface: insert/search/keyword_search).

### Retrieval (hybrid, and CPU-friendly)
`_search_vector_store()` (main.py line 1584): semantic search (over-fetch 4×) + `keyword_search` BM25 + **entity boosts**, fused in `references/mem0/mem0/utils/scoring.py`: BM25 raw scores squashed through a **query-length-adaptive sigmoid** (`get_bm25_params`, line 16), then additive `combined = (semantic + bm25 + 0.5·entity) / max_possible` with the semantic threshold gating candidacy (`score_and_rank`, line 60). Entity extraction is **spaCy `en_core_web_sm`, no LLM** (`mem0/utils/entity_extraction.py` — proper nouns, quoted text, noun compounds); BM25 tokens are lemmatized (`mem0/utils/lemmatization.py`).

### Minimal viable version of the pipeline
Four pieces, ~200 lines of our own Python: (1) the ADDITIVE extraction prompt (trimmed) run through the dispatcher's headless Claude; (2) a `memories` SQLite table + FTS5 mirror; (3) MD5-hash dedup + prompt-side "existing memories" dedup; (4) the append-only `history` table. The ADD/UPDATE/DELETE prompt becomes a **nightly reconciliation pass** rather than a per-turn call — cheaper on subscription budget and exactly fits our systemd-timer model.

---

## 2. graphiti — temporal knowledge graph

### Schema (fully portable, plain Pydantic)
- `references/graphiti/graphiti_core/nodes.py`: **`EpisodicNode`** (line 318: raw `content`, `source`, `valid_at` = when the event occurred, `entity_edges`), **`EntityNode`** (line 499: `name`, `name_embedding`, `summary` = LLM-maintained digest of surrounding edges, free-form `attributes`, `labels` for entity types), **`CommunityNode`** (line 687: `summary` of member nodes), **`SagaNode`** (line 867: rolling narrative over an episode sequence — their newest addition).
- `references/graphiti/graphiti_core/edges.py`: **`EntityEdge`** (line 263) is the heart: `name` (SCREAMING_SNAKE relation), **`fact`** (natural-language sentence — this is what gets embedded and searched), `fact_embedding`, `episodes` (provenance uuids), and the **bi-temporal triple: `valid_at` (fact became true), `invalid_at` (fact stopped being true), `expired_at` (when the SYSTEM learned it's no longer current)**. `EpisodicEdge` = MENTIONS (episode→entity).

### Extraction prompts
`references/graphiti/graphiti_core/prompts/`:
- `extract_nodes.py` — `extract_message` (line 83): entities from a conversation turn given previous episodes for pronoun resolution; also `classify_nodes`, `extract_attributes`, `extract_summary` (per-entity summary refresh).
- `extract_edges.py` — `edge` (line 96): fact triples between the already-extracted ENTITIES, with `valid_at`/`invalid_at` resolved against `REFERENCE_TIME`; `extract_timestamps[_batch]` as a separate lightweight call when dates are missing.
- `dedupe_nodes.py` — LLM resolves whether an extracted entity IS an existing entity (aided by a non-LLM fuzzy pre-pass in `utils/maintenance/dedup_helpers.py` — normalized-name exact match + MinHash/Jaccard shortlist).
- `dedupe_edges.py` — **`resolve_edge`** (line 43): given EXISTING FACTS + FACT INVALIDATION CANDIDATES (continuous idx), the LLM returns `duplicate_facts` and `contradicted_facts` in one shot. The worked examples (duplicate vs "same relationship, updated title = contradiction not duplicate" vs "different events on different days = neither") are the distilled hard-won logic.

### Invalidation (the part everyone gets wrong)
`references/graphiti/graphiti_core/utils/maintenance/edge_operations.py`, **`resolve_edge_contradictions()`** (line 538): pure-Python, ~35 lines, no DB dependency — a contradicted older edge gets `invalid_at = new_edge.valid_at` and `expired_at = now()` only if their validity intervals actually overlap. This ports to SQLite verbatim. Ingestion order confirmed in `graphiti.py::add_episode` (line 980): retrieve previous episodes → extract nodes → resolve/dedupe nodes → extract edges → resolve edges + invalidate → build MENTIONS edges → persist → optional community update.

### Hybrid retrieval
`references/graphiti/graphiti_core/search/`: `search_config.py` — per-object-type method lists: **cosine similarity, BM25 fulltext, breadth-first graph traversal (`bfs_max_depth`)**, then a reranker: **`rrf`** (reciprocal rank fusion, `search_utils.py` line 1780, ~15 lines), `mmr` (line 1901), `node_distance` (rerank by graph hops from a center node, line 1798), `episode_mentions` (frequency), or `cross_encoder`. `search_config_recipes.py` has ready-made combos (e.g. `COMBINED_HYBRID_SEARCH_RRF`). The BM25 side is the graph DB's Lucene/FTS index — in our port it's SQLite FTS5; BFS is a recursive CTE.

### Community detection
`references/graphiti/graphiti_core/utils/maintenance/community_operations.py` — **`label_propagation()`** (line 93) is pure Python over an adjacency projection (~40 lines, no Neo4j), followed by LLM summarization of each cluster (`prompts/summarize_nodes.py`). Portable as-is.

### Neo4j vs SQLite verdict
Requires a graph DB only for: Cypher storage/queries, DB-side fulltext indexes, and DB-side vector indexes (drivers: `driver/neo4j_driver.py`, `falkordb_driver.py`, **`kuzu_driver.py` — embedded, file-based**, `neptune_driver.py`). Kuzu proves the schema fits an embedded store; its DDL (`kuzu_driver.py` lines 56–128) is effectively a relational schema already — 4 node tables + 3 rel tables. **Everything conceptual ports to SQLite**: `episodes`, `entities`, `edges(source_id, target_id, name, fact, valid_at, invalid_at, expired_at, episode_ids)`, `mentions(episode_id, entity_id)`. Graph traversal at our scale (one user, thousands of nodes) is a recursive CTE or a Python BFS over an in-memory adjacency dict — we don't need a graph engine.

---

## 3. letta — MemGPT core/archival memory + sleep-time agents

### Core-memory blocks
- `references/letta/letta/schemas/block.py` — a **Block** = `label` ("human", "persona", or arbitrary), `value` (text), `limit` (char budget, enforced), `description`, `read_only` flag. `DEFAULT_BLOCKS = [Human, Persona]` (line 131).
- `references/letta/letta/schemas/memory.py` — `Memory.compile()` (line 688) renders all blocks into the system prompt inside `<memory_blocks>` tags with their limits shown, so they're **always in-context**.
- The canonical MemGPT system prompt explaining the whole pattern to the agent: `references/letta/letta/prompts/system_prompts/memgpt_chat.py` — three tiers: **core memory** (in-context, self-edited), **recall memory** (searchable full conversation history), **archival memory** (infinite, explicit search required).

### Self-editing tools
`references/letta/letta/functions/function_sets/base.py`:
`core_memory_append` (line 246), `core_memory_replace` (line 263, exact-string replace like our Edit tool), `rethink_memory` (line 283, wholesale rewrite of one block), plus the newer line-oriented set `memory_replace`/`memory_insert`/`memory_apply_patch`/`memory_rethink`/`memory_finish_edits` (lines 311–520), and `archival_memory_insert(content, tags)` / `archival_memory_search(query, tags, start_datetime, end_datetime)` (lines 164/194), `conversation_search` (line 87).

### Sleep-time agents (background consolidation — present and mature)
- System prompt: `references/letta/letta/prompts/system_prompts/sleeptime_v2.py` — a dedicated background agent whose ONLY job is editing the primary agent's memory blocks: multi-step precise edits or `rethink`, "convert 'today'/'recently' to absolute dates because memory persists indefinitely", "not every observation warrants an edit", finish tool when done.
- Orchestration: `references/letta/letta/groups/sleeptime_multi_agent_v2.py` — after each main-agent step, if `turns_counter % sleeptime_agent_frequency == 0` (`schemas/group.py` line 43), fire the sleep-time agent as a background task with the recent transcript.
- Voice variant (directly relevant to Jarvis): `references/letta/letta/prompts/system_prompts/voice_sleeptime.py` + `agents/voice_sleeptime_agent.py` — two-phase: segment older transcript into topical chunks → `store_memories` to archival, then refine the human block.

### Storage
`references/letta/letta/orm/passage.py` — archival passages: `text`, `embedding`, `tags` (JSON), `metadata`, timestamps. On the SQLite backend letta uses **`sqlite-vec`** (`orm/sqlite_functions.py` — `serialize_float32`, cosine distance UDF) — production proof that sqlite-vec works for exactly this workload. Embeddings: OpenAI `text-embedding-3-small` default, `letta-free` hosted, and **Ollama local embedding models** (`schemas/providers/ollama.py` line 138).

---

## 4. Local-first, CPU-only feasibility

What the repos actually use: mem0 → OpenAI default but ships **`huggingface.py` embedder defaulting to `sentence-transformers/multi-qa-MiniLM-L6-cos-v1`** (~80 MB, 384-d) and **`fastembed.py`** (ONNX runtime, default `thenlper/gte-large`, but fastembed's catalog includes `BAAI/bge-small-en-v1.5`, ~34 MB int8-quantized ONNX, 384-d). graphiti → OpenAI/Gemini/Voyage embedders only, BUT its cross-encoder reranker `cross_encoder/bge_reranker_client.py` runs **local sentence-transformers `BAAI/bge-reranker`** on CPU. letta → sqlite-vec + Ollama local embeddings supported.

**Verdict: entirely realistic on our CPU-only box.**
- **Embeddings**: `BAAI/bge-small-en-v1.5` int8 ONNX (~34 MB, 384-d) or `all-MiniLM-L6-v2` (~80 MB) via onnxruntime — single-sentence embed is ~5–15 ms on CPU; a nightly batch of a few hundred memories is seconds. This joins our existing warm-model roster (whisper small.en int8, Silero, openWakeWord are all already ONNX/CTranslate2 on CPU).
- **Vector store**: `sqlite-vec` (single C extension, loadable into our existing `data/mission.db`) — letta proves it. 384-d floats × even 50k memories ≈ 75 MB; brute-force KNN at this scale is instant.
- **BM25**: SQLite **FTS5 is already built in** — zero new dependencies. mem0's sigmoid-normalized BM25 + additive fusion transfers directly to FTS5's bm25() scores.
- **Fallback ladder**: FTS5-only (no embeddings at all) is a legitimate v2.0; add sqlite-vec + ONNX embedder as v2.1. Graphiti's RRF makes fusion trivial either way.
- **Extraction LLM**: zero extra cost path — all extraction prompts run through the dispatcher's headless `claude -p` (locked decision #1). No local extraction LLM needed. spaCy `en_core_web_sm` (~12 MB) optional for mem0-style entity boosts, or skip it and let Claude tag entities during extraction.
- New deps to ask the user about (per working-style rule): `sqlite-vec`, `onnxruntime` + a tokenizer (or `fastembed`), optionally `spacy`.

---

## 5. Recommended minimal memory design for our stack

One SQLite file (extend `data/mission.db`), the existing vault, and Claude-as-extractor via dispatcher headless runs on systemd timers. **Anchor per memory type:**

| Memory type | Anchor pattern | Why |
|---|---|---|
| **Personal** (preferences, family, habits, coding style) | **letta core blocks** — markdown files `vault/memory/{human,persona,projects,style}.md`, each with a char budget, injected into every dispatcher system prompt | Always-in-context beats retrieval for the ~2 KB that matters most; blocks are human-editable markdown (our vault philosophy) |
| **Per-project** (goals/todos/architecture/decisions) | **letta blocks + our existing vault layout** — one block file per project under `vault/projects/<name>.md`, loaded when the area/project matches | Same mechanism, scoped injection; zero new machinery |
| **Episodic** ("yesterday I worked on auth") | **graphiti episodes** — an `episodes` table (source, content, `valid_at` = occurrence time) written by every dispatcher interaction + daily-brief; facts carry `valid_at/invalid_at/expired_at` | Bi-temporal columns are the one thing you cannot retrofit later; capture from day one |
| **Semantic search** ("that MongoDB optimization 4 months ago") | **mem0 hybrid retrieval** — FTS5 BM25 + sqlite-vec cosine, fused with graphiti's RRF (simpler than mem0's sigmoid scoring; upgrade later if needed) | Both signals are cheap; RRF is 15 lines |
| **Knowledge graph** (Ayra —works_on→ Startup —uses→ FastAPI) | **graphiti entities/edges in SQLite** — `entities`, `edges` (with the fact sentence + bi-temporal fields), `mentions`; traversal via recursive CTE; graphiti's dedupe_edges prompt for contradiction handling | Facts-as-sentences means the graph IS also the semantic index — edges get embedded, one search surface |
| **Consolidation** | **letta sleep-time pattern on our timers** — nightly `mission-memory-consolidate` timer: dispatcher headless run reads the day's episodes, runs mem0's additive-extraction prompt + graphiti's resolve_edge prompt, edits block files with our existing `Edit(vault/memory/**)` allowedTools scoping | Maps 1:1 onto the daily-brief agent we already run; the agent literally uses Claude Code's own edit tools as letta's memory tools |

Flow: interactions append raw episodes cheaply (no LLM) → nightly consolidation extracts facts/entities/edges, dedupes, invalidates, updates core blocks → retrieval endpoint in the dispatcher (`/memory/search`) does BM25+vector+1-hop-graph with RRF, available to voice, dashboard, and agent runs alike.

---

## 6. Ranked liftable patterns

| # | Pattern | Source | What it does | Maps to | Effort |
|---|---|---|---|---|---|
| 1 | **Additive extraction prompt** (obs-date grounding, linked_memory_ids, anti-hallucination int ids) | `references/mem0/mem0/configs/prompts.py` lines 468–1062 | Conversation → rich self-contained dated memories in one LLM call | New `areas/memory/agents/consolidate.md` prompt run by dispatcher headless | **S** |
| 2 | **Bi-temporal edge schema + pure-Python invalidation** | `references/graphiti/graphiti_core/edges.py` line 263; `utils/maintenance/edge_operations.py` line 538 | `valid_at/invalid_at/expired_at` + interval-overlap contradiction expiry | New `edges` table in mission.db + ~40 lines in a new `dispatcher/memory.py` | **S** |
| 3 | **Hybrid retrieval + RRF** | `references/graphiti/graphiti_core/search/search_utils.py` line 1780 (rrf), line 1901 (mmr); `references/mem0/mem0/utils/scoring.py` (sigmoid BM25 alternative) | Fuse FTS5 BM25 + sqlite-vec ranks | Dispatcher `/memory/search` endpoint | **M** (S if FTS5-only first) |
| 4 | **Sleep-time consolidation agent** | `references/letta/letta/prompts/system_prompts/sleeptime_v2.py`; trigger pattern in `letta/groups/sleeptime_multi_agent_v2.py` line 114 | Background agent edits memory blocks with precise edit tools; "absolute dates only"; "finish when nothing to do" | Nightly systemd timer + `scripts/run_agent.py`, `Edit(vault/memory/**)` scope | **S** (we own all the infra already) |
| 5 | **Core-memory blocks with char budgets** | `references/letta/letta/schemas/block.py`; compile pattern in `schemas/memory.py` line 688; MemGPT tiering in `prompts/system_prompts/memgpt_chat.py` | Always-in-context labeled blocks, size-capped, self-edited | Markdown files under `vault/memory/`, injected by `dispatcher/quick.py` + runner system prompts | **S** |
| 6 | **Edge dedupe/contradiction prompt** | `references/graphiti/graphiti_core/prompts/dedupe_edges.py` line 43 | duplicate_facts vs contradicted_facts in one call, with the tricky examples | Part of nightly consolidation prompt | **S** |
| 7 | **ADD/UPDATE/DELETE/NONE reconciliation prompt** | `references/mem0/mem0/configs/prompts.py` line 176 + `get_update_memory_messages` line 406 | LLM reconciles new facts against retrieved old ones with stable ids | Weekly deep-clean pass over the `memories` table (pairs with weekly-review timer) | **S** |
| 8 | **SQLite history/audit table + hash dedup** | `references/mem0/mem0/memory/storage.py` line 108; hash dedup in `mem0/memory/main.py` lines 961–980 | Append-only event log per memory; MD5 dedup pre-LLM | Same table shape in mission.db; matches our runs-table habits | **S** |
| 9 | **sqlite-vec integration pattern** | `references/letta/letta/orm/sqlite_functions.py` | float32 serialization + cosine UDF for SQLite vectors | `dispatcher/memory.py` storage layer (v2.1) | **M** (new dep — ask first) |
| 10 | **Label-propagation communities + summaries** | `references/graphiti/graphiti_core/utils/maintenance/community_operations.py` line 93; `prompts/summarize_nodes.py` | Pure-Python clustering, then LLM summaries per cluster | Monthly timer producing `vault/memory/topics.md` | **M** — defer until graph has real mass |
| 11 | **Voice sleep-time chunking** | `references/letta/letta/prompts/system_prompts/voice_sleeptime.py` | Segment older transcript into topical chunks with context blurbs before archiving | Jarvis conversation-history archiving (pairs with our quick-path resume sessions) | **S** — later |
| 12 | **spaCy entity boosts (no-LLM)** | `references/mem0/mem0/utils/entity_extraction.py`, `lemmatization.py` | CPU-only entity + lemma extraction for retrieval boosts | Optional retrieval enhancer | **M** — only if recall proves weak |

## 7. Explicit SKIPs

- **graphiti as a dependency / any graph DB (Neo4j, FalkorDB, Neptune, even embedded Kuzu)** — locked SQLite decision; our scale needs no graph engine; recursive CTE suffices. Kuzu's DDL is still useful as schema reference only.
- **mem0's 30 vector-store providers, server/, openmemory/, MCP plumbing** — cloud/multi-tenant machinery; we are one user behind one dispatcher.
- **letta's runtime entirely** (agent loop, ORM/alembic/Postgres, `server/`, multi-agent groups) — it's a competing agent OS; we lift its *memory shapes and prompts* only. Its per-N-turns sleeptime trigger is replaced by our nightly timer (subscription budget).
- **Cloud embeddings** (OpenAI/Voyage/Gemini defaults in all three) — no API credentials on this machine by design; local ONNX or FTS5-only instead.
- **Cross-encoder reranking** (`references/graphiti/graphiti_core/cross_encoder/`) — a second warm model for marginal gain at our corpus size; RRF first.
- **mem0 graph-memory module** (`mem0/graphs/` — Cypher-generation against Neo4j/Memgraph) — graphiti's design is strictly better and portable.
- **mem0 `PROCEDURAL_MEMORY_SYSTEM_PROMPT`** (prompts.py line 326) — verbatim agent-trajectory logging; our runs table + session logs already cover this.
- **Per-turn synchronous extraction** (mem0's default `add()` behavior) — adds an LLM call to every voice interaction; latency + budget hostile. Nightly batch instead; the raw episode append is free.
- **letta sagas/summarization cascade** (SagaNode, `summarize_sagas.py`) — new and complex; our daily briefs are already rolling summaries.

**Suggested build order**: episodes table + core blocks injection (immediate value, no new deps) → nightly consolidation agent with prompts #1/#6 → FTS5 search endpoint → sqlite-vec + ONNX embedder (after dep approval) → graph edges + invalidation → communities. Every step is independently testable and each maps onto infrastructure (dispatcher, timers, vault, runs table) that already exists and is verified.
