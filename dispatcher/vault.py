"""Mechanical vault file access for the dashboard: list tasks, toggle status,
fetch the current brief. Plain file I/O — no Claude involved (task *capture*
and anything requiring judgment stays on the dispatch path).
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from .queue_watcher import parse_task_file

log = logging.getLogger("dispatcher.vault")


def _task_files(vault: Path):
    return sorted((vault / "tasks").glob("*.md"))


def list_tasks(vault: Path) -> list[dict]:
    out = []
    for f in _task_files(vault):
        try:
            meta, body = parse_task_file(f.read_text())
        except Exception:
            # one hand-mangled frontmatter must not take down the whole list
            log.warning("skipping unparseable task file %s", f.name)
            continue
        out.append({
            "file": f.name,
            "title": meta.get("title", f.stem),
            "status": meta.get("status", "open"),
            "due": str(meta["due"]) if meta.get("due") else None,
            "created": str(meta.get("created", "")) or None,
            "source": meta.get("source"),
            "notes": body or None,
        })
    # open first; within a group, dated before undated, earliest due first
    out.sort(key=lambda t: (t["status"] != "open", t["due"] is None, t["due"] or "", t["title"]))
    return out


def toggle_task(vault: Path, filename: str) -> dict:
    tasks_dir = (vault / "tasks").resolve()
    path = (tasks_dir / filename).resolve()
    if path.parent != tasks_dir or path.suffix != ".md":
        raise ValueError("invalid task filename")
    if not path.exists():
        raise FileNotFoundError(filename)
    text = path.read_text()
    if "status: open" in text:
        new = "done"
        text = text.replace("status: open", "status: done", 1)
    else:
        new = "open"
        text = text.replace("status: done", "status: open", 1)
    path.write_text(text)
    return {"file": filename, "status": new}


def current_brief(vault: Path) -> dict | None:
    briefs = vault / "briefs"
    today = briefs / f"{date.today().isoformat()}.md"
    path = today if today.exists() else None
    if path is None:
        candidates = sorted(briefs.glob("*.md"), key=lambda p: p.stat().st_mtime)
        path = candidates[-1] if candidates else None
    if path is None:
        return None
    return {"file": path.name, "content": path.read_text(), "is_today": path == today}
