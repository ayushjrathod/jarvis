"""Drop-a-markdown-file task queue. Files in queue/*.md with optional YAML
frontmatter (mode, area, ...) become tasks; processed files move to
queue/.processed/<name>.<task_id>.md, unparseable ones to queue/.failed/.

Transient failures (disk hiccup, dispatcher momentarily overloaded) are
retried in place for up to `max_retries` polls before falling back to
.failed; terminal failures (bad frontmatter, empty body) move to .failed
immediately.
"""

from __future__ import annotations

import asyncio
import logging

import yaml

log = logging.getLogger("dispatcher.queue")

# Whitelist: only these are treated as transient (worth retrying). Anything
# else (YAML errors, empty body, bad task shape) is terminal on first try.
TRANSIENT_EXCEPTIONS = (OSError, ConnectionError, TimeoutError)

DEFAULT_MAX_RETRIES = 3


async def _ingest_one(f, service, processed, failed, retries: dict, max_retries: int) -> None:
    try:
        meta, body = parse_task_file(f.read_text())
        if not body:
            raise ValueError("empty task body")
        task = await service.submit(
            text=body,
            source="queue",
            mode=meta.get("mode", "auto"),
            area=meta.get("area"),
            metadata=meta or None,
        )
        f.rename(processed / f"{f.stem}.{task['id']}.md")
        retries.pop(f.name, None)
        log.info("queued %s as task %s (%s)", f.name, task["id"], task["kind"])
    except Exception as exc:
        if isinstance(exc, TRANSIENT_EXCEPTIONS):
            n = retries.get(f.name, 0) + 1
            if n <= max_retries:
                retries[f.name] = n
                log.warning(
                    "transient error ingesting %s (attempt %d/%d), will retry: %s",
                    f.name, n, max_retries, exc,
                )
                return
            log.error("giving up on %s after %d transient retries: %s", f.name, max_retries, exc)
        else:
            log.exception("failed to ingest %s", f.name)
        retries.pop(f.name, None)
        try:
            f.rename(failed / f.name)
        except OSError:
            pass


def parse_task_file(text: str) -> tuple[dict, str]:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            meta = yaml.safe_load(parts[1]) or {}
            if not isinstance(meta, dict):
                meta = {}
            return meta, parts[2].strip()
    return {}, text.strip()


async def watch(service):
    cfg = service.cfg
    qdir = cfg.queue_dir
    processed = qdir / ".processed"
    failed = qdir / ".failed"
    max_retries = getattr(cfg, "queue_max_retries", DEFAULT_MAX_RETRIES)
    retries: dict[str, int] = {}
    # never let one bad scan kill the watcher: it runs as a fire-and-forget
    # task nobody observes, so an escaped exception would silently stop queue
    # ingestion while the dispatcher keeps looking healthy
    while True:
        try:
            for d in (qdir, processed, failed):
                d.mkdir(parents=True, exist_ok=True)
            for f in sorted(qdir.glob("*.md")):
                await _ingest_one(f, service, processed, failed, retries, max_retries)
        except Exception:
            log.exception("queue scan failed; retrying next poll")
        await asyncio.sleep(cfg.poll_interval_s)
