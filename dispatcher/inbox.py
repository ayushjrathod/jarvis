"""Event-driven proactivity: react when a file lands in vault/inbox/.

Everything proactive in the system so far is *time*-triggered (timers, the
automations scheduler). This is the first thing that reacts to the world
changing: drop a PDF, an article export or a bookmark dump into `vault/inbox/`
and it gets indexed and acknowledged without anyone asking.

Design notes:

- **Polling, not inotify.** The dispatcher is already up and already polls
  (queue watcher, automations scheduler), a minute of latency is irrelevant for
  "I saved a file", and inotify would mean a dependency plus a whole class of
  watch-descriptor edge cases on a directory the user syncs into.
- **Two-phase settle.** A file fires only once its (mtime, size) is unchanged
  between two consecutive polls, so a large PDF still being written — or a
  half-finished sync — isn't indexed at half length.
- **Seeded on first pass.** The files already sitting in the inbox at startup
  are recorded, not fired, so a dispatcher restart doesn't re-announce
  everything.
- **Deterministic by default.** Arrival triggers a reindex and a notification,
  which costs nothing. Summarizing the document needs a model, so it's opt-in
  (`inbox.summarize`) — cheap surprises are still surprises, and this session
  spent its first hour cutting a $0.14 recurring cost nobody had noticed.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

log = logging.getLogger("dispatcher.inbox")

DEFAULT_INTERVAL_S = 60
SUMMARIZE_PROMPT = (
    "A new file just arrived in my inbox at {path}. Read it and reply with ONE "
    "short sentence saying what it is and why I might care. No preamble."
)


def scan(directory: Path) -> dict[str, tuple[float, int]]:
    """{relative name: (mtime, size)} for regular files, README excluded.
    Missing directory scans as empty rather than raising — the inbox is a
    convention, not a requirement."""
    out: dict[str, tuple[float, int]] = {}
    try:
        entries = sorted(directory.rglob("*"))
    except OSError:
        log.exception("inbox scan failed for %s", directory)
        return out
    for f in entries:
        try:
            if not f.is_file() or f.name.startswith("."):
                continue
            if f.name.lower() == "readme.md":
                continue
            st = f.stat()
            out[str(f.relative_to(directory))] = (st.st_mtime, st.st_size)
        except OSError:
            continue          # vanished mid-scan; it'll show up next poll
    return out


class InboxWatcher:
    """Pure state machine over successive scans — `step` is the whole policy,
    which is what makes this testable without touching a filesystem or a clock."""

    def __init__(self):
        self.seen: dict[str, tuple[float, int]] = {}
        self.pending: dict[str, tuple[float, int]] = {}
        self.seeded = False

    def step(self,
             current: dict[str, tuple[float, int]]) -> tuple[list[str], list[str]]:
        """(arrived, removed). `arrived` is files that appeared AND stopped
        changing — those get announced. `removed` is reported separately
        because a deleted file needs a reindex to drop its rows (otherwise it
        stays searchable until the next restart) but is not worth a
        notification. Both empty on the first call, which only records what
        was already there."""
        if not self.seeded:
            self.seen = dict(current)
            self.seeded = True
            return [], []

        fired: list[str] = []
        for name, stamp in current.items():
            if self.seen.get(name) == stamp:
                continue                      # known and unchanged
            if self.pending.get(name) == stamp:
                fired.append(name)            # stable across two polls
                self.seen[name] = stamp
                self.pending.pop(name, None)
            else:
                self.pending[name] = stamp    # new or still being written
        for n in [n for n in self.pending if n not in current]:
            self.pending.pop(n, None)
        removed = [n for n in self.seen if n not in current]
        for n in removed:
            self.seen.pop(n, None)
        return sorted(fired), sorted(removed)


async def watch(svc):
    """Poll the inbox; on arrival reindex, then notify (and optionally
    summarize). Never dies on one bad iteration — this loop lives for the
    process's whole lifetime."""
    from . import embeddings, ingest, notify

    cfg = svc.cfg
    icfg = cfg.inbox or {}
    interval = max(5, icfg.get("check_interval_s", DEFAULT_INTERVAL_S))
    directory = (cfg.root / icfg.get("dir", "vault/inbox")).resolve()
    watcher = InboxWatcher()
    log.info("inbox watcher: %s every %ss", directory, interval)

    while True:
        try:
            arrived, removed = watcher.step(await asyncio.to_thread(scan, directory))
            if arrived or removed:
                if arrived:
                    log.info("inbox: %d new file(s): %s", len(arrived),
                             ", ".join(arrived[:5]))
                if removed:
                    # no notification for a deletion, but the index must drop
                    # its rows or the file stays searchable after it's gone
                    log.info("inbox: %d file(s) removed: %s", len(removed),
                             ", ".join(removed[:5]))
                try:
                    stats = await asyncio.to_thread(
                        ingest.ingest_vault, svc.db, cfg.root,
                        cfg.memory.get("index_dirs", ["vault"]))
                    log.info("inbox reindex: %s", stats)
                    await asyncio.to_thread(embeddings.embed_missing, cfg, svc.db)
                except Exception:
                    log.exception("inbox reindex failed")

                if icfg.get("summarize"):
                    for name in arrived:
                        await _summarize(svc, directory / name)
                elif icfg.get("notify", True):
                    what = (arrived[0] if len(arrived) == 1
                            else f"{len(arrived)} files")
                    summary = f"Indexed {what} from your inbox — searchable now."
                    await notify.send_desktop(summary)
                    await svc.hooks.fire({
                        "event": "notify", "task_id": None, "kind": "inbox",
                        "source": "inbox", "text": ", ".join(arrived[:5]),
                        "speech": summary, "summary": summary,
                    })
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("inbox watcher iteration failed")
        await asyncio.sleep(interval)


async def _summarize(svc, path: Path):
    """Opt-in one-liner about a new document. Read-only tools, scoped to the
    file; the notify gate decides whether it's worth interrupting for."""
    try:
        task = await svc.create_task(
            SUMMARIZE_PROMPT.format(path=path), "inbox", "quick", None,
            {"task_type": "inbox-summarize",
             "allowed_tools": ["Read", "Glob", "Grep"]},
            trusted=True)
        await svc._drain_quick(task)
    except Exception:
        log.exception("inbox summarize failed for %s", path)
