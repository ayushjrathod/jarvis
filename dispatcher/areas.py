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

log = logging.getLogger("dispatcher.areas")


def _subsequence(trigger: str, words: list[str]) -> bool:
    """True if the trigger's words appear in order (gaps allowed) in `words` —
    "mark task done" matches "mark the domain task as done". Spoken phrasing
    never matches exact substrings reliably."""
    needles = trigger.split()
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


def _parse_skill(path: Path) -> Area | None:
    text = path.read_text()
    meta, body = {}, text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            try:
                meta = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                log.warning("bad frontmatter in %s", path)
                return None
            body = parts[2].strip()
    return Area(
        name=meta.get("name", path.parent.name),
        path=path.parent,
        triggers=[t.lower() for t in meta.get("triggers", [])],
        quick_triggers=[t.lower() for t in meta.get("quick_triggers", [])],
        allowed_tools=meta.get("allowed_tools", []),
        quick_allowed_tools=meta.get("quick_allowed_tools", []),
        skill_body=body,
    )


class AreaRegistry:
    def __init__(self, areas_dir: Path):
        self.areas_dir = Path(areas_dir)

    def load(self) -> dict[str, Area]:
        areas = {}
        if self.areas_dir.is_dir():
            for skill in sorted(self.areas_dir.glob("*/SKILL.md")):
                area = _parse_skill(skill)
                if area:
                    areas[area.name] = area
        return areas

    def get(self, name: str) -> Area | None:
        return self.load().get(name)

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
