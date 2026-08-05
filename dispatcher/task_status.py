"""One shared reader for task-file status.

Three parsers used to disagree about what "open" means: brief.py matched
`^status: open$` case-insensitively, vault.toggle_task did an exact-substring
search, and list_tasks YAML-parses with a default of "open". A file readable
as open by one and done by another is how the brief skipped real work while
the dashboard showed it done (or vice versa). Every reader here goes through
`parse_task_file` semantics: the frontmatter `status` key, defaulting to
"open", compared case-insensitively after stripping.
"""

from __future__ import annotations

import re

# A status line inside YAML frontmatter: case/space tolerant, multiline so it
# can be found anywhere — callers restrict it to the frontmatter block.
STATUS_LINE = re.compile(r"(?m)^(?P<indent>\s*)status\s*:\s*(?P<value>\S+)\s*$")


def frontmatter_end(text: str) -> int | None:
    """Index of the closing `---` for a `---`-led file, else None."""
    if not text.startswith("---"):
        return None
    end = text.find("---", 3)
    return end if end != -1 else None


def current_status(meta: dict) -> str:
    """Status per the parsed frontmatter dict, defaulting like list_tasks."""
    return str(meta.get("status", "open")).strip().lower()


def is_open_text(text: str) -> bool:
    """Mechanical open-check over raw file text (timer-safe: never raises)."""
    try:
        if text.startswith("---"):
            end = text.find("---", 3)
            head = text if end == -1 else text[:end]
            m = STATUS_LINE.search(head)
            if m:
                return m.group("value").strip().lower() == "open"
            return True  # no status line reads as open, like list_tasks
        return True
    except Exception:
        return True  # unreadable: assume it matters, let the agent look


def set_status(text: str, new: str) -> str:
    """Return `text` with the frontmatter status set to `new` (insert if
    missing, create frontmatter if absent). Raises on unterminated block."""
    if text.startswith("---"):
        end = text.find("---", 3)
        if end == -1:
            raise ValueError("unterminated frontmatter")
        m = STATUS_LINE.search(text[:end])
        if m:
            return text[:m.start()] + f"{m.group('indent')}status: {new}\n" + text[m.end():]
        return text[:end] + f"status: {new}\n" + text[end:]
    return f"---\nstatus: {new}\n---\n{text}"
