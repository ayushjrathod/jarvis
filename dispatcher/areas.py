"""Life-area registry (locked decision #5: areas are plugins).

Each dir under areas/ holds a SKILL.md whose YAML frontmatter declares:
  name, triggers (phrases → agentic), quick_triggers (phrases → quick with
  read-only tools, streamed to voice), allowed_tools, quick_allowed_tools.
The SKILL.md body (+ optional CLAUDE.md alongside) is injected into runs as
system-prompt context. Adding an area = adding a directory; no core changes.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .queue_watcher import parse_task_file

log = logging.getLogger("dispatcher.areas")


# Tools that read nothing writable: allowed bare or scoped — a scope on one
# of these can only narrow what is already read-only.
_READ_ONLY = frozenset({"Read", "Glob", "Grep"})

# The only roots a scoped write grant may cover. vault/ holds memory, tasks
# and briefs (the three on-disk grants areas actually hold); areas/ lets the
# learn/reflection loop author skills; data/ covers the SQLite/index files.
_SCOPE_ROOTS = ("vault/", "areas/", "data/")


def _scope_is_safe(scope: str) -> bool:
    """A permission scope like `vault/tasks/**` that cannot escape its root:
    non-empty, relative, with no `..` segment, under an allowlisted root.
    `**`, `/abs/path`, `../../etc` and `~/x` all fail — positively, not by
    matching known-bad shapes."""
    s = scope.strip()
    if not s or s.startswith(("/", "~")):
        return False
    if ".." in s.split("/"):
        return False
    return s.startswith(_SCOPE_ROOTS)


def _is_privileged_tool(tool: str) -> bool:
    """A tool grant that must not appear in an untrusted area's frontmatter.

    Positive test, fail-closed: Bash in any form; the whole Edit/Write family
    (Edit, Write, MultiEdit, NotebookEdit — a bare name or an unsafe scope);
    and anything else that isn't a known read-only tool (MCP tools, Task,
    WebFetch, future names). The old test read "contains a parenthesis" as
    "is scoped", so `Edit(**)` sailed through and a reflection run could
    escalate to repo-wide write — then to config.yaml, then to real Bash."""
    base, _, arg = tool.partition("(")
    base = base.strip()
    if base == "Bash":
        return True
    if base in _READ_ONLY:
        return False
    if base.endswith("Edit") or base.endswith("Write"):
        # Write(path) rules are silently ignored by the CLI, so stripping one
        # changes nothing at runtime — and keeping the family uniform means
        # the next *Edit-shaped tool is closed by default, not open.
        arg = arg.strip()
        if not arg or not arg.endswith(")"):
            return True
        return not _scope_is_safe(arg[:-1])
    return True


def _sanitize_tools(tools: list, dir_name: str, privileged: set) -> list:
    """Strip privilege-escalating grants (Bash, unscoped Edit/Write) from a
    loaded area's tool list unless the area is config-blessed (H2). SKILL.md
    frontmatter is the privilege manifest, and a reflection/learn run editing
    areas/** could be prompt-injected into writing Bash into it — load-time
    validation makes such a grant inert. Non-string entries pass through
    untouched (same tolerance as trigger parsing).

    `dir_name` is the area's DIRECTORY name, never the frontmatter's `name:` —
    the operator blesses a directory in security.privileged_areas, and the
    frontmatter lives inside the very file whose grants are being checked
    (see _parse_skill, 2026-08-10)."""
    if not tools or dir_name in privileged:
        return tools
    safe = []
    for t in tools:
        if isinstance(t, str) and _is_privileged_tool(t):
            log.warning("area %r: stripped privileged tool grant %r at load "
                        "(add %r to security.privileged_areas to allow it)",
                        dir_name, t, dir_name)
            continue
        safe.append(t)
    return safe


# An agent reference off the wire names a file we read; keep it a bare slug so
# it can never climb out of areas/<area>/agents/.
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _subsequence(trigger: str, words: list[str]) -> bool:
    """True if the trigger's words appear in order (gaps allowed) in `words` —
    "mark task done" matches "mark the domain task as done". Spoken phrasing
    never matches exact substrings reliably."""
    needles = trigger.split()
    if not needles:
        return False
    i = 0
    for w in words:
        if w == needles[i]:
            i += 1
            if i == len(needles):
                return True
    return False


@dataclass
class Area:
    name: str
    path: Path
    triggers: list = field(default_factory=list)
    quick_triggers: list = field(default_factory=list)
    allowed_tools: list = field(default_factory=list)
    quick_allowed_tools: list = field(default_factory=list)
    skill_body: str = ""

    def context(self) -> str:
        parts = [f"# Area: {self.name}\n{self.skill_body}"]
        claude_md = self.path / "CLAUDE.md"
        if claude_md.exists():
            parts.append(claude_md.read_text())
        return "\n\n".join(parts)


def _parse_skill(path: Path, privileged: set = frozenset()) -> Area | None:
    # The whole parse is guarded, not just the YAML: load() runs on EVERY
    # POST /task (match() → Service.route(), re-read from disk, uncached), and
    # nothing on that path catches. So one SKILL.md that is unreadable
    # (PermissionError) or not valid UTF-8 (UnicodeDecodeError) used to 500
    # every single dispatch — no attacker needed, since reflection and learn
    # runs write these files and an interrupted write is enough. One bad area
    # now drops out exactly like bad frontmatter already did (2026-08-10).
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError) as e:
        log.warning("skipping unreadable area skill %s: %s", path, e)
        return None
    try:
        # One frontmatter reader for the whole dispatcher (parse_task_file):
        # this used to be a fifth copy of the same split, and copies drift —
        # the H2 bypass lived in exactly such a copy. strict=True keeps the
        # stood-down contract below: a non-mapping frontmatter skips the area
        # instead of loading a neutered skill.
        meta, body = parse_task_file(text, strict=True)
    except yaml.YAMLError as e:
        log.warning("skipping area skill %s: bad frontmatter: %s", path, e)
        return None
    except ValueError as e:
        log.warning("skipping area skill %s: %s", path, e)
        return None

    def phrases(key):
        # tolerate hand-edited frontmatter: drop empty/non-string entries
        # rather than letting one of them 500 every /task at match time
        return [t.lower() for t in meta.get(key) or [] if isinstance(t, str) and t.strip()]

    # Two different names on purpose. `name` is the display/routing key the
    # frontmatter chooses; the privilege decision keys on the DIRECTORY, which
    # is what the operator actually blessed in security.privileged_areas. They
    # used to be the same value, so `areas/notes-helper/SKILL.md` declaring
    # `name: tasks` inherited whatever "tasks" was blessed for — a manifest
    # granting itself privilege from inside the file being validated, which is
    # exactly the escalation H2 exists to stop (2026-08-10).
    dir_name = path.parent.name
    name = meta.get("name", dir_name)
    if not isinstance(name, str) or not name.strip():
        name = dir_name   # a non-string name would become a bogus telemetry key
    return Area(
        name=name,
        path=path.parent,
        triggers=phrases("triggers"),
        quick_triggers=phrases("quick_triggers"),
        allowed_tools=_sanitize_tools(meta.get("allowed_tools", []), dir_name,
                                      privileged),
        quick_allowed_tools=_sanitize_tools(
            meta.get("quick_allowed_tools", []), dir_name, privileged),
        skill_body=body,
    )


class AreaRegistry:
    def __init__(self, areas_dir: Path, privileged_areas=None):
        self.areas_dir = Path(areas_dir)
        # areas allowed to keep Bash / unscoped Edit-Write frontmatter grants
        self.privileged = frozenset(privileged_areas or [])

    def load(self) -> dict[str, Area]:
        areas = {}
        if self.areas_dir.is_dir():
            for skill in sorted(self.areas_dir.glob("*/SKILL.md")):
                area = _parse_skill(skill, self.privileged)
                if area:
                    areas[area.name] = area
        return areas

    def get(self, name: str) -> Area | None:
        return self.load().get(name)

    def agent_tools(self, area_name: str, agent: str) -> list[str] | None:
        """allowed_tools declared by areas/<area>/agents/<agent>.md, read from
        disk and sanitized like SKILL.md.

        Timer agents (daily-brief, weekly-review) need grants the area itself
        doesn't carry — the brief writes vault/briefs/**, the tasks area only
        grants vault/tasks/**. Those grants used to ride in on the submitting
        client's metadata, which the H1 trust boundary now strips: the timers
        silently lost their write access and three briefs were never written.
        Resolving the file server-side restores them WITHOUT reopening the
        hole, because the privilege manifest is on-disk repo content (the same
        trust level as SKILL.md) rather than anything the caller supplies.
        Returns None when there's no such agent or it declares no tools."""
        if not area_name or not agent:
            return None
        if not (_SLUG_RE.match(area_name) and _SLUG_RE.match(agent)):
            log.warning("ignoring agent tool lookup for %r/%r: not a bare slug",
                        area_name, agent)
            return None
        path = self.areas_dir / area_name / "agents" / f"{agent}.md"
        if not path.is_file():
            log.warning("no agent file at %s; falling back to area tools", path)
            return None
        try:
            text = path.read_text()
            if not text.startswith("---"):
                return None
            parts = text.split("---", 2)
            if len(parts) != 3:
                return None
            meta = yaml.safe_load(parts[1]) or {}
        except (OSError, yaml.YAMLError):
            log.exception("unreadable agent frontmatter in %s", path)
            return None
        tools = meta.get("allowed_tools")
        if not isinstance(tools, list) or not tools:
            return None
        # `area_name` here IS the directory name — it was just used to build the
        # path we read — so the privilege key is already the operator-blessed
        # one, unlike the frontmatter `name:` _parse_skill used to pass
        # (checked 2026-08-10 while fixing that).
        return _sanitize_tools(tools, area_name, self.privileged)

    def match(self, text: str) -> tuple[Area | None, str | None]:
        """Returns (area, kind_hint). quick_triggers win over triggers —
        they're the more specific read-aloud phrases."""
        words = re.findall(r"[a-z']+", text.lower())
        areas = self.load().values()
        for area in areas:
            if any(_subsequence(p, words) for p in area.quick_triggers):
                return area, "quick"
        for area in areas:
            if any(_subsequence(p, words) for p in area.triggers):
                return area, "agentic"
        return None, None
