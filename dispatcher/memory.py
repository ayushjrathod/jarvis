"""Core memory blocks + consolidation hand-off (Phase F).

Blocks follow letta's core-memory design (Apache-2.0, references/letta:
always-in-context, bounded, edited by a background agent rather than per-turn)
with char budgets per Hermes (MIT, references/hermes-agent
tools/memory_tool.py). USER.md = user profile, MEMORY.md = assistant notes;
plain markdown in vault/memory/ that the user may edit freely. Budgets are
enforced at read time so a hand-edited file can't blow up every prompt.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import yaml

from .config import Config
from .db import Database, now

log = logging.getLogger("dispatcher.memory")

DEFAULT_BUDGETS = {"USER.md": 2000, "MEMORY.md": 3200}
DEFAULT_BUDGET = 2000
# Only these files are injected into every prompt. A note dropped into
# blocks_dir (or an OCR/ingest artifact that lands there) must NOT silently
# enter every system prompt — that's an injection + context-bloat vector (M4).
# Override with memory.block_files in config; per-project notes live in
# blocks_dir/projects/ and are searchable, not auto-injected.
DEFAULT_BLOCK_FILES = ("USER.md", "MEMORY.md")

BLOCKS_PREAMBLE = (
    "Persistent memory blocks — personal context distilled from past "
    "interactions, updated nightly. Trust them, but the user's live words "
    "win on conflict."
)


def _cfg_memory(cfg: Config) -> dict:
    return getattr(cfg, "memory", None) or {}


def blocks_dir(cfg: Config) -> Path:
    d = _cfg_memory(cfg).get("blocks_dir", "vault/memory")
    p = Path(d)
    return p if p.is_absolute() else cfg.root / p


def blocks_context(cfg: Config) -> str:
    """Render top-level block files into one system-prompt section. Missing
    dir or empty files → empty string (injection is a no-op until the user
    or the consolidation agent writes something)."""
    d = blocks_dir(cfg)
    if not d.is_dir():
        return ""
    budgets = {**DEFAULT_BUDGETS, **(_cfg_memory(cfg).get("block_budgets") or {})}
    allow = set(_cfg_memory(cfg).get("block_files") or DEFAULT_BLOCK_FILES)
    parts = []
    for f in sorted(d.glob("*.md")):
        if f.name not in allow:  # arbitrary top-level .md never gets injected
            continue
        try:
            text = f.read_text(errors="replace").strip()
        except OSError as e:
            log.warning("memory block %s unreadable: %s", f, e)
            continue
        if not text:
            continue
        budget = budgets.get(f.name, DEFAULT_BUDGET)
        if len(text) > budget:
            text = text[:budget] + "\n[…truncated: over the block budget]"
        parts.append(f"## {f.name}\n{text}")
    if not parts:
        return ""
    return "<memory_blocks>\n" + BLOCKS_PREAMBLE + "\n\n" + "\n\n".join(parts) + "\n</memory_blocks>"


def should_capture(cfg: Config, task: dict, status: str) -> bool:
    mcfg = _cfg_memory(cfg)
    if not mcfg.get("capture", True) or status == "cancelled":
        return False
    if task["source"] in (mcfg.get("capture_exclude_sources") or []):
        return False
    try:
        meta = json.loads(task["metadata"]) if task.get("metadata") else {}
    except (TypeError, ValueError):
        meta = {}
    # meta-work must not feed back into memory: the consolidator's own run
    # would become next night's input, reflections are private review passes,
    # and the Phase I gate/parse runs are plumbing around real interactions.
    #
    # daily-brief/weekly-review belong here for exactly the same reason, and
    # were missed until 2026-08-01. Their episode says "I wrote a file", so the
    # nightly graph extractor kept turning the system's own housekeeping into
    # "knowledge": every fact in the graph was a self-observation like "the
    # daily-brief automation continued writing successfully through 07-31",
    # each night invalidating the previous night's copy of itself. Nothing is
    # lost by excluding them — a brief's *content* reaches memory the right
    # way, through the vault index that ingests vault/briefs/ already.
    return meta.get("task_type") not in (
        "memory-consolidate", "reflection", "notify-gate", "automation-parse",
        "graph-extract", "graph-reconcile", "daily-brief", "weekly-review")


def build_consolidation(cfg: Config, db: Database) -> dict | None:
    """Export unconsolidated episodes to data/consolidation/<ts>.md and build
    the agent task from areas/memory/agents/consolidate.md. Returns None when
    there is nothing to consolidate. The caller marks the episodes and submits
    the task; the export file stays on disk as the audit trail (a failed run
    can be replayed against it by hand)."""
    episodes = db.unconsolidated_episodes()
    if not episodes:
        return None

    stamp = now().replace(":", "").replace("+0000", "Z")
    export = cfg.root / "data" / "consolidation" / f"{stamp}.md"
    export.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Episode export — {now()}", ""]
    for e in episodes:
        head = (f"## Episode {e['id']} — {e['valid_at']} · {e['source']}"
                f" · {e['kind']} · {e['status']}")
        if e.get("area"):
            head += f" · area:{e['area']}"
        lines += [head, f"**User:** {e['user_text']}", ""]
        if e.get("assistant_text"):
            lines += [f"**Jarvis:** {e['assistant_text']}", ""]
    export.write_text("\n".join(lines))

    prompt_path = cfg.root / "areas" / "memory" / "agents" / "consolidate.md"
    text = prompt_path.read_text()
    meta = {}
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            meta = yaml.safe_load(parts[1]) or {}
            text = parts[2].strip()
    rel = str(export.relative_to(cfg.root))
    text = text.replace("{{EPISODES_FILE}}", rel).replace("{{DATE}}", now()[:10])

    return {
        "text": text,
        "episode_ids": [e["id"] for e in episodes],
        "export_path": rel,
        "metadata": {
            "task_type": meta.get("task_type", "memory-consolidate"),
            "allowed_tools": meta.get("allowed_tools"),
        },
    }
