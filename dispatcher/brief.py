"""Deterministic pre-check for the daily brief.

The brief agent costs ~$0.40 and 8-11 turns per run. When the vault has no open
tasks and no recently-touched notes it spends all of that to write one
paragraph saying "clean slate" — which is what it wrote every day from
2026-07-24 to 08-01, because the vault holds one task (done, 07-06) and two
notes (07-05). Costs on the subscription are notional, but the *quota* is real
and the plan cap has taken the assistant down twice (sessions 7 and 10).

So: check the vault mechanically first, and on a quiet day write the file here
for nothing. Same shape as `spotify.detect` / `desktop.detect` / the inbox
watcher — deterministic by default, model only when there's something to think
about. A day with any open task or any fresh note still gets the real agent.

This deliberately does NOT try to summarise anything. If there is material,
it defers; if there isn't, there is nothing to summarise.
"""

from __future__ import annotations

import re
import time
from datetime import date
from pathlib import Path

# `status: open` in a task file's YAML frontmatter. Matched with a regex rather
# than a YAML parse because one malformed task file must not take out the
# timer — the same reasoning as the /vault/tasks endpoint's per-file guard.
_STATUS_OPEN = re.compile(r"^\s*status\s*:\s*open\s*$", re.I | re.M)

DEFAULT_NOTES_DAYS = 3


def open_tasks(root: Path) -> list[Path]:
    out = []
    for p in sorted((Path(root) / "vault" / "tasks").glob("*.md")):
        try:
            if _STATUS_OPEN.search(p.read_text(errors="replace")):
                out.append(p)
        except OSError:
            out.append(p)      # unreadable: assume it matters, let the agent look
    return out


def recent_notes(root: Path, days: int = DEFAULT_NOTES_DAYS) -> list[Path]:
    cutoff = time.time() - days * 86400
    out = []
    for p in sorted((Path(root) / "vault" / "notes").glob("*.md")):
        try:
            if p.stat().st_mtime >= cutoff:
                out.append(p)
        except OSError:
            continue
    return out


def is_quiet(root: Path, days: int = DEFAULT_NOTES_DAYS) -> bool:
    """Nothing for a brief to report: no open tasks, no fresh notes."""
    return not open_tasks(root) and not recent_notes(root, days)


def render_quiet(day: date, days: int = DEFAULT_NOTES_DAYS) -> str:
    """The brief for a day with no material, in the agent's own format.

    Written to be read aloud (TTS) like the real one: plain sentences, no
    links, no tables."""
    return (
        f"# Brief — {day.isoformat()}\n\n"
        "## Today\n"
        "Nothing on the books — no open tasks and no notes touched in the last "
        f"{days} days. The day is yours to direct.\n\n"
        "## Open tasks\n"
        "No open tasks.\n\n"
        "## Recent notes\n"
        f"No notes updated in the last {days} days.\n"
    )


def write_quiet(root: Path, day: date, days: int = DEFAULT_NOTES_DAYS) -> Path:
    path = Path(root) / "vault" / "briefs" / f"{day.isoformat()}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_quiet(day, days))
    return path
