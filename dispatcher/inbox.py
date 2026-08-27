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
- **Summarizing is capped and additive** (2026-08-08). It costs one cold
  `claude -p` per file — ~$0.10-0.14 at the documented floor — on a directory
  the user *syncs* into, so `inbox.summarize_max_per_poll` bounds the fan-out
  and the overflow is logged and named in the notification rather than dropped
  silently. And it no longer replaces the free "Indexed X" notice: it used to
  sit in an `elif`, so switching summaries on switched the one thing that costs
  nothing off.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

log = logging.getLogger("dispatcher.inbox")

DEFAULT_INTERVAL_S = 60
# how many new files one poll will spend a model on; see `_summarize_cap`
DEFAULT_SUMMARIZE_MAX = 5
SUMMARIZE_PROMPT = (
    "A new file just arrived in my inbox at {path}. Read it and reply with ONE "
    "short sentence saying what it is and why I might care. No preamble."
)


def arrival_summary(arrived: list[str], stats: dict | None,
                    skipped: int = 0) -> str:
    """The honest one-liner for an inbox arrival, derived from the REINDEX —
    never from the arrival list. The old code announced 'Indexed N files —
    searchable now' over whatever landed, so a contract.docx/csv/eml that
    indexed nothing was still celebrated, and a raised reindex still
    notified success. stats=None means the reindex raised."""
    what = arrived[0] if len(arrived) == 1 else f"{len(arrived)} files"
    if stats is None:
        return (f"Couldn't index {what} from your inbox — "
                "will retry on the next pass.")
    added = stats.get("added", 0)
    if added > 0:
        s = f"Indexed {what} from your inbox — searchable now."
    else:
        why = []
        if stats.get("unsupported"):
            why.append(f"{stats['unsupported']} unsupported format(s)")
        if stats.get("dep_gated"):
            why.append(f"{stats['dep_gated']} need PDF/OCR deps")
        if stats.get("unparseable"):
            why.append(f"{stats['unparseable']} couldn't be parsed")
        detail = (" (" + ", ".join(why) + ")") if why else ""
        s = (f"{what} from your inbox arrived but nothing new is searchable"
             f"{detail}.")
    if skipped:
        s += (f" Summarizing {len(arrived) - skipped}; "
              f"{skipped} skipped by the per-poll cap.")
    return s


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


def _summarize_cap(value) -> int:
    """`inbox.summarize_max_per_poll`, parsed so no config value can throw.
    Absent/blank/junk falls back to the default; 0 means "index but never
    spend a model", which is a legitimate thing for an operator to want."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return DEFAULT_SUMMARIZE_MAX


async def watch(svc):
    """Poll the inbox; on arrival reindex, then notify (and optionally
    summarize). Never dies on one bad iteration — this loop lives for the
    process's whole lifetime."""
    from . import embeddings, ingest, notify

    cfg = svc.cfg
    icfg = cfg.inbox or {}
    # `or DEFAULT`, not `.get(key, DEFAULT)` (fixed 2026-08-08): a bare
    # "check_interval_s:" in config.yaml parses as None, and max(5, None) is a
    # TypeError raised out here — *outside* the loop's try, on a fire-and-forget
    # task nobody awaits — so one blank config line made the watcher silently
    # never start. Everything read up here has to fail toward a default.
    interval = max(5, icfg.get("check_interval_s") or DEFAULT_INTERVAL_S)
    summarize_cap = _summarize_cap(icfg.get("summarize_max_per_poll"))
    # Same blank-key shape as the scheduler's interval (fixed there
    # 2026-08-27): a bare `dir:` parses as None and (root / None) is a
    # TypeError before the first poll.
    directory = (cfg.root / (icfg.get("dir") or "vault/inbox")).resolve()
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
                    stats: dict | None = await asyncio.to_thread(
                        ingest.ingest_vault, svc.db, cfg.root,
                        cfg.memory.get("index_dirs", ["vault"]))
                    log.info("inbox reindex: %s", stats)
                    await asyncio.to_thread(embeddings.embed_missing, cfg, svc.db)
                except Exception:
                    log.exception("inbox reindex failed")
                    stats = None

                # `arrived` only — a removal reindexes but is never announced.
                # Without this guard a deleted file notified "Indexed 0 files
                # from your inbox", since the outer branch fires on either list.
                if arrived:
                    # Summarizing is now *additive*, not an `elif` (2026-08-08).
                    # It used to replace this notice, so turning it on traded
                    # the free announcement for per-file summaries that were
                    # then delivered nowhere — see _summarize.
                    todo, skipped = [], []
                    if icfg.get("summarize"):
                        todo, skipped = (arrived[:summarize_cap],
                                         arrived[summarize_cap:])
                    if skipped:
                        # No silent caps. This is a synced directory: a 50-file
                        # sync was 50 serialized cold `claude -p` runs at the
                        # ~$0.10-0.14 floor, and the plan cap has taken the
                        # assistant out twice. Bound it, then say what it cost.
                        log.warning(
                            "inbox: summarize capped at %d per poll — "
                            "summarizing %s; skipped %s", summarize_cap,
                            ", ".join(todo) or "nothing", ", ".join(skipped))
                    if icfg.get("notify", True):
                        summary = arrival_summary(arrived, stats, len(skipped))
                        await notify.send_desktop(summary)
                        await svc.hooks.fire({
                            "event": "notify", "task_id": None, "kind": "inbox",
                            "source": "inbox", "text": ", ".join(arrived[:5]),
                            "speech": summary, "summary": summary,
                        })
                    for name in todo:
                        await _summarize(svc, directory / name)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("inbox watcher iteration failed")
        await asyncio.sleep(interval)


async def _summarize(svc, path: Path):
    """Opt-in one-liner about a new document, delivered the same way the
    deterministic notice above is.

    It used to `_drain_quick` and drop the answer on the floor (fixed
    2026-08-08), and nothing downstream picked it up either: source "inbox"
    isn't in `automations.notify_sources`, so `notify.surfacing` returns
    "surface" rather than "gate"; the resulting `done` event carries
    kind="quick"; and the voice brain only speaks `kind == "agentic"`. So the
    feature charged a cold `claude -p` per file and produced silence. Delivery
    is explicit now — `_collect_quick` (the helper the notify gate and the
    automation parser use for machine-consumed quick output) plus the same
    send_desktop + `notify` hook event, which every client already speaks
    because it keys on event == "notify".
    """
    from . import notify

    try:
        task = await svc.create_task(
            SUMMARIZE_PROMPT.format(path=path), "inbox", "quick", None,
            {"task_type": "inbox-summarize",
             "allowed_tools": ["Read", "Glob", "Grep"]},
            trusted=True)
        answer, status = await svc._collect_quick(task)
        # one spoken sentence: collapse the whitespace a model reply arrives with
        summary = " ".join((answer or "").split())[:400]
        if status != "done" or not summary:
            log.warning("inbox summarize for %s settled %s with no text",
                        path.name, status)
            return
        await notify.send_desktop(summary)
        await svc.hooks.fire({
            "event": "notify", "task_id": task["id"], "kind": "inbox",
            "source": "inbox", "text": path.name,
            "speech": summary, "summary": summary,
        })
    except Exception:
        log.exception("inbox summarize failed for %s", path)
