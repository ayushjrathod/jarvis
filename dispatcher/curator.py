"""Deterministic skill curator (Phase G): ages area skills
active → stale → archived from usage telemetry. Lifecycle rules after
hermes-agent's curator.apply_automatic_transitions (MIT,
references/hermes-agent agent/curator.py):

- stale after `stale_after_days` with no use and no patch;
- archived (dir moved to areas/.archive/, recoverable — never deleted) after
  a further `archive_after_days` stale;
- exempt: pinned skills, protected names (core areas), timer-referenced
  areas (a systemd timer submits into them), and never-used skills younger
  than the stale window — use_count=0 is absence of evidence, not evidence
  of staleness.

The LLM consolidation pass Hermes layers on top is deliberately not built:
they ship it off by default and it's tuned for hundreds of skills.
No git operations — the move shows up in `git status` for the user's review.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config
from .db import Database

log = logging.getLogger("dispatcher.curator")

DEFAULT_STALE_DAYS = 30
DEFAULT_ARCHIVE_DAYS = 90
# areas a systemd timer submits into — archiving one would break the timer
TIMER_AREAS = {"tasks", "memory"}


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def run(db: Database, cfg: Config) -> dict:
    lcfg = (getattr(cfg, "learning", None) or {}).get("curator", {})
    stale_after = timedelta(days=lcfg.get("stale_after_days", DEFAULT_STALE_DAYS))
    archive_after = timedelta(days=lcfg.get("archive_after_days", DEFAULT_ARCHIVE_DAYS))
    protected = set(lcfg.get("protected", [])) | TIMER_AREAS
    now = datetime.now(timezone.utc)

    areas_dir = cfg.root / "areas"
    on_disk = {d.name for d in areas_dir.iterdir()
               if d.is_dir() and not d.name.startswith(".")} if areas_dir.is_dir() else set()
    usage = {u["name"]: u for u in db.skill_usage_all()}

    report = {"stale": [], "archived": [], "skipped": []}
    for name in sorted(on_disk):
        u = usage.get(name)
        if name in protected or (u and u.get("pinned")):
            report["skipped"].append(name)
            continue
        if u is None:
            # on disk but never dispatched to — start its clock now
            db.set_skill_state(name, "active")
            continue
        last_activity = max(filter(None, (
            _parse(u.get("last_used_at")), _parse(u.get("last_patched_at")),
            _parse(u.get("first_seen_at")))), default=None)
        if last_activity is None:
            continue
        idle = now - last_activity
        state = u.get("state", "active")
        if state == "active" and idle > stale_after:
            db.set_skill_state(name, "stale")
            report["stale"].append(name)
        elif state == "stale":
            if idle > stale_after + archive_after:
                target = areas_dir / ".archive" / name
                target.parent.mkdir(exist_ok=True)
                shutil.move(str(areas_dir / name), str(target))
                db.set_skill_state(name, "archived")
                report["archived"].append(name)
                log.warning("curator: archived area %s -> %s", name, target)
            # record_skill_use/patch already flip stale->active on activity
    return report
