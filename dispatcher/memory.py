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

from .areas import AreaRegistry
from .config import Config
from .db import Database, local_today, now

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

# Task types that are the system talking to itself. None of them is an
# interaction with the user, so none may become an episode: an episode is read
# back by the consolidation agent and the nightly graph extractor as "a day's
# interaction", and whatever is in it becomes "knowledge".
#
#  - memory-consolidate / reflection: the housekeeping passes themselves; the
#    consolidator's own run would be next night's input.
#  - notify-gate / automation-parse / media-parse / inbox-summarize: Phase I/
#    media/inbox plumbing wrapped around a real interaction. Their user_text is
#    a machine-written prompt, not a question anyone asked.
#  - graph-extract / graph-reconcile: same, one layer down.
#  - daily-brief / weekly-review: the episode says "I wrote a file" (missed
#    until 2026-08-01 — every fact in the graph had become a self-observation
#    like "the daily-brief automation continued writing successfully through
#    07-31", each night invalidating the previous night's copy of itself).
#    Nothing is lost: a brief's *content* reaches memory the right way, through
#    the vault index, which ingests vault/briefs/ already.
#  - divert: only machine-submitted diverts (a fired automation, a queue file)
#    get a task row at all — the HTTP path streams and records nothing — so
#    capturing them would feed the graph one identical "play jazz / Playing
#    jazz" episode every morning forever. A human's music command is not lost;
#    it never produced an episode in the first place.
#
# media-parse and inbox-summarize were added 2026-08-08, having drifted off
# this list while sitting on every other one. Live proof in data/mission.db,
# episode 62: a media-parse prompt ("Convert the user's music request into a
# Spotify search… Reply with ONE JSON object") stored as user_text, already
# consolidated — i.e. the graph extractor has read a JSON format instruction as
# something the user said. Exactly the bug 5f200f3 fixed for the timer agents.
#
# SIBLING LISTS — the same idea, split across modules, and it has drifted twice
# now. A new internal task type must be added to all of these by hand:
#   dispatcher/service.py  NO_CONTINUITY_SOURCES     (quick-session chaining;
#                                                     keyed on source, not type)
#   dispatcher/service.py  DEFAULT_META_TASK_TYPES   (cheap-model routing)
#   dispatcher/notify.py   NEVER_SURFACE_TASK_TYPES  (never announced)
#   dispatcher/notify.py   NEVER_GATE_TASK_TYPES     (never sent to the gate)
# They are deliberately not one shared set — graph-extract wants a real model,
# a divert wants announcing — and service.py imports this module, so a shared
# constant would have to move somewhere neither imports. Keep them in sync by
# reading this comment.
INTERNAL_TASK_TYPES = (
    "memory-consolidate", "reflection", "notify-gate", "automation-parse",
    "media-parse", "inbox-summarize", "graph-extract", "graph-reconcile",
    "daily-brief", "weekly-review", "divert",
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
    # meta-work must not feed back into memory — see INTERNAL_TASK_TYPES above
    # for what counts as meta-work and why each entry is on the list.
    return meta.get("task_type") not in INTERNAL_TASK_TYPES


def _write_new(path: Path, text: str, tries: int = 50) -> Path:
    """Write `text` to `path`, or to `<stem>-N<suffix>` if that name is taken.
    Returns the path actually written. Exclusive-create ("x") rather than
    exists()-then-write, so concurrent callers can't both pick the same name."""
    for n in range(tries):
        p = path if n == 0 else path.with_name(f"{path.stem}-{n}{path.suffix}")
        try:
            with open(p, "x") as fh:
                fh.write(text)
            return p
        except FileExistsError:
            continue
    # 50 exports in one second is not a real workload; overwrite rather than
    # fail the consolidation outright.
    log.warning("consolidation export: %d collisions on %s; overwriting",
                tries, path)
    path.write_text(text)
    return path


def render_episodes(episodes: list[dict]) -> str:
    """The markdown the consolidation agent and the graph extractor both read.

    Shared (2026-08-11) so the graph can be handed a DIFFERENT subset than the
    consolidation export holds: the two hand-offs are marked independently, and
    after a rolled-back consolidation the next pass re-exports episodes the
    extractor has already digested."""
    lines = [f"# Episode export — {now()}", ""]
    for e in episodes:
        head = (f"## Episode {e['id']} — {e['valid_at']} · {e['source']}"
                f" · {e['kind']} · {e['status']}")
        if e.get("area"):
            head += f" · area:{e['area']}"
        lines += [head, f"**User:** {e['user_text']}", ""]
        if e.get("assistant_text"):
            lines += [f"**Jarvis:** {e['assistant_text']}", ""]
    return "\n".join(lines)


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
    # Exclusive create, uniquified on collision (2026-08-10). `stamp` has
    # one-second resolution and write_text overwrites, so two /memory/consolidate
    # calls landing in the same second both built a job and the second export
    # CLOBBERED the first — destroying the only replay artefact a failed
    # consolidation has, for the batch that most needed it. "x" makes the loser
    # of the race take the next name instead of the winner's file; there is no
    # check-then-write gap to lose.
    export = _write_new(export, render_episodes(episodes))

    prompt_path = cfg.root / "areas" / "memory" / "agents" / "consolidate.md"
    text = prompt_path.read_text()
    meta = {}
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            meta = yaml.safe_load(parts[1]) or {}
            text = parts[2].strip()
    rel = str(export.relative_to(cfg.root))
    text = text.replace("{{EPISODES_FILE}}", rel).replace("{{DATE}}", local_today())

    # Grants come through AreaRegistry, NOT from the frontmatter we just parsed
    # for the body (H2). This task is submitted trusted=True, so an unsanitized
    # `allowed_tools:` here would survive the trust boundary intact — and the
    # agent file lives under areas/**, which reflection and learn runs can edit.
    # That is precisely the escalation the load-time sanitizer exists to stop:
    # a prompt-injected reflection writing `- Bash` into consolidate.md would
    # otherwise hand the nightly run a shell.
    registry = AreaRegistry(cfg.root / "areas",
                            privileged_areas=cfg.privileged_areas)
    return {
        "text": text,
        "episode_ids": [e["id"] for e in episodes],
        "export_path": rel,
        "metadata": {
            "task_type": meta.get("task_type", "memory-consolidate"),
            "allowed_tools": registry.agent_tools("memory", "consolidate"),
        },
    }
